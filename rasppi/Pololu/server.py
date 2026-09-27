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

# 速度に関する設定（マリオカート風: コマンド受信直後は最高速の20%、5秒で最高速の100%まで加速）
MIN_SPEED_PERCENT = 20
MAX_SPEED_PERCENT = 100
RAMP_UP_SECONDS = 5.0

# ステアリングの不感帯（この範囲内の傾きは「倒していない」とみなす）
STEER_DEADZONE = 0.1

CONTROL_INTERVAL_SEC = 0.05  # モーター制御ループの周期（クライアントからの次のコマンドを待つ間もこの周期で駆動し続ける）


def clamp_speed(speed):
    try:
        speed = int(round(float(speed)))
    except (TypeError, ValueError):
        speed = 0
    return max(0, min(100, speed))


def calc_ramped_speed(held_seconds):
    """同じコマンドを受信し続けた時間から目標速度(%)を算出する（受信直後20% → 5秒で100%）"""
    ratio = min(max(held_seconds / RAMP_UP_SECONDS, 0.0), 1.0)
    return MIN_SPEED_PERCENT + (MAX_SPEED_PERCENT - MIN_SPEED_PERCENT) * ratio


def calc_wheel_speeds(base_speed, stick_x):
    """Lスティックの左右方向の傾きから左右モーターの速度(%)を算出する

    例: base_speed=100, stick_x=0.5 (右に半分倒す) の場合、
        右に曲がろうとしているとみなし右タイヤの速度をさらに1/2にする。
    """
    left_speed = base_speed
    right_speed = base_speed

    if abs(stick_x) > STEER_DEADZONE:
        # 倒した量が大きいほど、内側のタイヤの速度を落とす
        inner_ratio = max(0.0, 1.0 - abs(stick_x))
        if stick_x > 0:
            # 右に倒している -> 右折 -> 右タイヤを減速
            right_speed = base_speed * inner_ratio
        else:
            # 左に倒している -> 左折 -> 左タイヤを減速
            left_speed = base_speed * inner_ratio

    return left_speed, right_speed


class RealMotorController:
    """dual_max14870_rpi の motors（pigpio 経由でPololu Dual MAX14870を制御）をラップする"""

    def __init__(self, motors, max_speed):
        self.motors = motors
        self.max_speed = max_speed

    def _to_signed_speed(self, direction, speed_percent):
        speed_percent = clamp_speed(speed_percent)
        value = int(round(speed_percent / 100 * self.max_speed))
        # 配線・モーターの向きによっては符号が逆になる場合があるので、
        # 実機で前後が逆に動く場合はここのプラスマイナスを入れ替える
        return -value if direction == "backward" else value

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
        left_speed = clamp_speed(left_speed)
        right_speed = clamp_speed(right_speed)
        print(f"[Motor] {direction.upper()} L={left_speed}% R={right_speed}%")

    def stop(self):
        print("[Motor] STOP")


try:
    from dual_max14870_rpi import motors as _motors, MAX_SPEED as _MAX_SPEED
    motor = RealMotorController(_motors, _MAX_SPEED)
    print("[Motor] Hardware driver (Pololu Dual MAX14870) initialized.")
except Exception as e:
    print(f"[Motor] Hardware driver unavailable ({e}). Using dummy driver.")
    motor = DummyMotorController()


# 簡易カメラジェネレーター（実機ではCamera Module 3からの映像を流し込む）
def generate_frames():
    if not cv2_imported:
        return
    cap = cv2.VideoCapture(0) # ラズパイのカメラデバイス
    while True:
        success, frame = cap.read()
        if not success:
            break
        else:
            _, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.get("/video_feed")
async def video_feed():
    """カメラ映像のストリーミング配信エンドポイント"""
    return StreamingResponse(generate_frames(), media_type="multipart/x-mixed-replace; boundary=frame")


class DriveState:
    """クライアントから最後に受信した操作状態（cmd/stick_x）と、その状態になった時刻を保持する"""

    def __init__(self):
        self.cmd = "stop"
        self.stick_x = 0.0
        self.direction_started_at = None

    def update(self, cmd, stick_x):
        if cmd != self.cmd:
            # 方向が切り替わった時だけ加速をやり直す（ステアリングだけの変化では加速を維持する）
            self.cmd = cmd
            self.direction_started_at = time.monotonic() if cmd != "stop" else None
        self.stick_x = stick_x


async def motor_control_loop(state: DriveState):
    """クライアントから次のコマンドが送られてくるまで、現在の操作状態を維持してモーターを駆動し続けるループ"""
    while True:
        if state.cmd == "stop":
            motor.stop()
        else:
            held_seconds = time.monotonic() - state.direction_started_at
            base_speed = calc_ramped_speed(held_seconds)
            left_speed, right_speed = calc_wheel_speeds(base_speed, state.stick_x)
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
