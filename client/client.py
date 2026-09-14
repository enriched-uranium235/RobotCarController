import asyncio
import json
import threading
import time
import cv2
import pygame
import websockets

# ラズパイのIPアドレスとポートを指定
RASPBERRY_PI_IP = "192.168.X.X"  # ←ご自身のラズパイのIPに変更してください
WS_URI = f"ws://{RASPBERRY_PI_IP}:8000/ws"
STREAM_URL = f"http://{RASPBERRY_PI_IP}:8000/video_feed"  # 将来的なカメラ配信用

# ゲームパッドのボタン/軸割り当て（お手元のコントローラーに合わせて調整してください）
BUTTON_A = 0  # 前進
BUTTON_B = 1  # 後退
AXIS_LSTICK_X = 0  # Lスティック 左右

# 速度に関する設定（マリオカート風: 押した瞬間は最高速の20%、5秒押し続けると最高速）
MIN_SPEED_PERCENT = 20
MAX_SPEED_PERCENT = 100
RAMP_UP_SECONDS = 5.0

# ステアリングの不感帯（この範囲内の傾きは「倒していない」とみなす）
STEER_DEADZONE = 0.1

SEND_INTERVAL_SEC = 0.05  # 20Hzでポーリング/送信

# 状態管理用
running = True


def camera_stream_worker():
    """別スレッドでカメラ映像を受信して表示する"""
    global running
    # ※ラズパイ側で映像配信のエンドポイントを用意した場合に機能します
    cap = cv2.VideoCapture(STREAM_URL)
    while running:
        ret, frame = cap.read()
        if ret:
            cv2.imshow("Robot Camera View", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        else:
            # 映像がまだ取れない場合のダミー表示やウェイト
            cv2.waitKey(100)
    cap.release()
    cv2.destroyAllWindows()


def calc_ramped_speed(held_seconds):
    """ボタンを押し続けた時間から目標速度(%)を算出する（押下直後20% → 5秒で100%）"""
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


async def ws_controller_loop():
    """WebSocketでゲームパッドの入力をラズパイへ送り続けるループ"""
    global running

    # pygame（ゲームパッド）の初期化
    pygame.init()
    pygame.joystick.init()

    joystick = None
    if pygame.joystick.get_count() > 0:
        joystick = pygame.joystick.Joystick(0)
        joystick.init()
        print(f"[Joystick] Connected: {joystick.get_name()}")
    else:
        print("[Joystick] Warning: No gamepads found. Robot will stay stopped.")

    async with websockets.connect(WS_URI) as ws:
        print("Connected to robot server.")

        direction = "stop"          # 現在の走行方向 ("forward" / "backward" / "stop")
        press_started_at = None     # 現在の方向のボタンを押し始めた時刻
        last_sent_payload = None

        while running:
            pygame.event.pump()

            a_pressed = bool(joystick.get_button(BUTTON_A)) if joystick else False
            b_pressed = bool(joystick.get_button(BUTTON_B)) if joystick else False
            stick_x = joystick.get_axis(AXIS_LSTICK_X) if joystick else 0.0

            now = time.monotonic()

            if a_pressed:
                new_direction = "forward"
            elif b_pressed:
                new_direction = "backward"
            else:
                new_direction = "stop"

            # 方向が切り替わった瞬間に押下開始時刻をリセット（=段階的な加速をやり直す）
            if new_direction != direction:
                direction = new_direction
                press_started_at = now if direction != "stop" else None

            if direction == "stop":
                left_speed, right_speed = 0, 0
            else:
                held_seconds = now - press_started_at
                base_speed = calc_ramped_speed(held_seconds)
                left_speed, right_speed = calc_wheel_speeds(base_speed, stick_x)

            payload = {
                "cmd": direction,
                "left_speed": round(left_speed),
                "right_speed": round(right_speed),
            }

            # 値に変化がある時だけ送信（無駄な送信を減らす）
            if payload != last_sent_payload:
                await ws.send(json.dumps(payload))
                last_sent_payload = payload
                print(f"Sent command: {payload}")

            await asyncio.sleep(SEND_INTERVAL_SEC)

    pygame.quit()


async def main():
    global running
    # カメラ受信用スレッドの起動
    cam_thread = threading.Thread(target=camera_stream_worker, daemon=True)
    cam_thread.start()

    try:
        await ws_controller_loop()
    except websockets.exceptions.ConnectionClosed:
        print("Connection closed by server.")
    finally:
        running = False

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user, exiting.")
        running = False
