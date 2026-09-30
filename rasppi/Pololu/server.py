import asyncio
import json
import time
cv2_imported = True
try:
    import cv2
except ImportError:
    cv2_imported = False

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
import uvicorn

app = FastAPI()

# ==========================================
# モータードライバーの初期化
# 実機（Pololu Dual MAX14870 + dual_max14870_rpi.py）が使えない環境ではダミーにフォールバックする
# ==========================================

# 移動速度（常に最高速で走行する。加減速ランプは行わない）
SPEED_PERCENT = 100

# ステアリングの不感帯（この範囲内の傾きは「倒していない」とみなす）
STEER_DEADZONE = 0.1

CONTROL_INTERVAL_SEC = 0.05  # モーター制御ループの周期（クライアントからの次のコマンドを待つ間もこの周期で駆動し続ける）


def clamp_speed(speed):
    try:
        speed = int(round(float(speed)))
    except (TypeError, ValueError):
        speed = 0
    return max(0, min(100, speed))


def calc_wheel_speeds(stick_x):
    """Lスティックの左右方向の傾きから左右モーターの速度(符号付き%)を算出する

    傾きが大きいほど内側のタイヤを減速し、さらに倒し切ると内側のタイヤを逆回転させて
    その場に近い旋回（信地旋回〜超信地旋回）ができるようにする。
    戻り値は SPEED_PERCENT に対する符号付きの値（負の値は逆回転を意味する）。

    例: stick_x=1.0 (右に最大まで倒す) の場合、
        右タイヤは-100（逆回転）、左タイヤは100のままとなり、その場で右に旋回する。
    """
    left_speed = SPEED_PERCENT
    right_speed = SPEED_PERCENT

    if abs(stick_x) > STEER_DEADZONE:
        # 不感帯を抜けた量を 0.0(不感帯境界) 〜 1.0(倒し切り) に正規化する
        turn_ratio = (abs(stick_x) - STEER_DEADZONE) / (1.0 - STEER_DEADZONE)
        # 内側のタイヤの比率を 1.0(直進と同じ速度) から -1.0(逆回転) まで線形に変化させる
        inner_ratio = 1.0 - 0.75 * turn_ratio
        if stick_x > 0:
            # 右に倒している -> 右折 -> 右タイヤを減速・逆回転
            right_speed = SPEED_PERCENT * inner_ratio
            left_speed = SPEED_PERCENT * 0.8
        else:
            # 左に倒している -> 左折 -> 左タイヤを減速・逆回転
            left_speed = SPEED_PERCENT * inner_ratio
            right_speed = SPEED_PERCENT * 0.8

    return left_speed, right_speed


class RealMotorController:
    """dual_max14870_rpi の motors（pigpio 経由でPololu Dual MAX14870を制御）をラップする"""

    def __init__(self, motors, max_speed):
        self.motors = motors
        self.max_speed = max_speed

    def _to_signed_speed(self, direction, speed_percent):
        # speed_percent が負の場合は、そのタイヤだけ direction と逆方向に回転させる
        # （信地旋回・超信地旋回でその場に近い旋回を行うため）
        magnitude = clamp_speed(abs(speed_percent))
        value = int(round(magnitude / 100 * self.max_speed))
        forward = (direction == "forward")
        if speed_percent < 0:
            forward = not forward
        # 配線・モーターの向きによっては符号が逆になる場合があるので、
        # 実機で前後が逆に動く場合はここのプラスマイナスを入れ替える
        return value if forward else -value

    def drive(self, direction, left_speed, right_speed):
        left_value = self._to_signed_speed(direction, left_speed)
        right_value = self._to_signed_speed(direction, right_speed)
        # motor1 = 左輪, motor2 = 右輪（配線が逆の場合は入れ替える）
        self.motors.setSpeeds(-left_value, right_value)

    def stop(self):
        self.motors.setSpeeds(0, 0)


class DummyMotorController:
    """実機が無い環境（開発PCなど）向けの動作確認用ダミー"""

    def drive(self, direction, left_speed, right_speed):
        # 負の値は逆回転（信地旋回・超信地旋回）を表すのでそのまま表示する
        print(f"[Motor] {direction.upper()} L={left_speed:.0f}% R={right_speed:.0f}%")

    def stop(self):
        print("[Motor] STOP")


try:
    from dual_max14870_rpi import motors as _motors, MAX_SPEED as _MAX_SPEED
    motor = RealMotorController(_motors, _MAX_SPEED)
    print("[Motor] Hardware driver (Pololu Dual MAX14870) initialized.")
except Exception as e:
    print(f"[Motor] Hardware driver unavailable ({e}). Using dummy driver.")
    motor = DummyMotorController()


# 簡易カメラジェネレーター
# Raspbian Buster + Camera Module 1 は legacy カメラスタックのみ対応のため、
# bcm2835-v4l2 カーネルモジュールで /dev/video0 を有効化した上で cv2.VideoCapture(0) を使う
# （事前に `sudo modprobe bcm2835-v4l2` が必要）
#
# カメラはモータードライバーと同様にサーバー起動時に一度だけ開き、プロセスが生きている間は
# 開いたままにする（bcm2835-v4l2 はクライアント接続ごとに open/close を繰り返すと
# 再オープンに失敗しやすく、クライアント再接続時に映像が戻らなくなるため）。
camera = None
if cv2_imported:
    _camera_candidate = cv2.VideoCapture(0)
    if _camera_candidate.isOpened():
        camera = _camera_candidate
        print("[Camera] /dev/video0 opened.")
    else:
        _camera_candidate.release()
        print("[Camera] Could not open /dev/video0. /video_feed will return an empty stream.")


def generate_frames():
    if camera is None:
        return
    while True:
        success, frame = camera.read()
        if not success:
            # 一時的な読み取り失敗ではカメラを閉じず、少し待って読み直す
            time.sleep(0.1)
            continue
        _, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.get("/video_feed")
async def video_feed():
    """カメラ映像のストリーミング配信エンドポイント"""
    return StreamingResponse(generate_frames(), media_type="multipart/x-mixed-replace; boundary=frame")


class DriveState:
    """クライアントから最後に受信した操作状態（cmd/stick_x）を保持する"""

    def __init__(self):
        self.cmd = "stop"
        self.stick_x = 0.0

    def update(self, cmd, stick_x):
        self.cmd = cmd
        self.stick_x = stick_x


async def motor_control_loop(state: DriveState):
    """クライアントから次のコマンドが送られてくるまで、現在の操作状態を維持してモーターを駆動し続けるループ"""
    while True:
        if state.cmd == "stop":
            motor.stop()
        else:
            left_speed, right_speed = calc_wheel_speeds(state.stick_x)
            motor.drive(state.cmd, left_speed, right_speed)
        await asyncio.sleep(CONTROL_INTERVAL_SEC)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    motor.stop()
    print("Client connected. Motor standby (STOP).")

    state = DriveState()
    control_task = asyncio.create_task(motor_control_loop(state))

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            cmd = message.get("cmd", "stop")
            stick_x = message.get("stick_x", 0.0)

            if cmd not in ("forward", "backward"):
                cmd = "stop"

            state.update(cmd, stick_x)

            await websocket.send_text(json.dumps({"status": f"processed_{cmd}"}))

    except WebSocketDisconnect:
        print("Client disconnected. Safety STOP.")
    finally:
        control_task.cancel()
        try:
            await control_task
        except asyncio.CancelledError:
            pass
        motor.stop()

if __name__ == "__main__":
    motor.stop()
    uvicorn.run(app, host="0.0.0.0", port=8000)
