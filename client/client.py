import asyncio
import json
import os
import threading

# ジョイスティック入力のみ使うため、SDLの映像ドライバをdummyに固定する。
# こうしないとpygame.init()がCocoaのメインメニュー操作を試み、別スレッドから呼んだ際にクラッシュする(macOS)。
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import cv2
import pygame
import websockets

# ラズパイのIPアドレスとポートを指定
RASPBERRY_PI_IP = "192.168.1.67"  # ←ご自身のラズパイのIPに変更してください
WS_URI = f"ws://{RASPBERRY_PI_IP}:8000/ws"
STREAM_URL = f"http://{RASPBERRY_PI_IP}:8000/video_feed"  # 将来的なカメラ配信用

# ゲームパッドのボタン/軸割り当て（お手元のコントローラーに合わせて調整してください）
BUTTON_A = 0  # 前進
BUTTON_B = 1  # 後退
AXIS_LSTICK_X = 0  # Lスティック 左右

# ステアリングの量子化しきい値（傾きを5段階の値に丸める。送信頻度は上がる）
STEER_DEADZONE = 0.1        # |x| <= 0.1                  -> 0.0（倒していないとみなす）
STEER_THRESHOLD_1 = 0.325   # 0.1   < |x| < 0.325          -> 0.25
STEER_THRESHOLD_2 = 0.55    # 0.325 <= |x| < 0.55          -> 0.5
STEER_THRESHOLD_3 = 0.775   # 0.55  <= |x| < 0.775         -> 0.75
                             # 0.775 <= |x| <= 1.0          -> 1.0

SEND_INTERVAL_SEC = 0.05  # 状態監視のポーリング間隔（実際の送信は状態が変化した時のみ）

# 状態管理用
running = True


def camera_stream_main():
    """メインスレッドでカメラ映像を受信して表示する

    macOSではcv2.imshow等のGUI表示はメインスレッドからしか呼び出せないため、
    表示処理はメインスレッドで行い、ゲームパッド/WebSocket通信を別スレッドに追い出している。
    """
    global running
    # ※ラズパイ側で映像配信のエンドポイントを用意した場合に機能します
    cap = cv2.VideoCapture(STREAM_URL)
    while running:
        ret, frame = cap.read()
        if ret:
            cv2.imshow("Robot Camera View", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                running = False
                break
        else:
            # 映像がまだ取れない場合のダミー表示やウェイト
            cv2.waitKey(100)
    cap.release()
    cv2.destroyAllWindows()


def quantize_stick(stick_x):
    """Lスティックの傾きを離散値へ量子化する（5段階: 0, 0.25, 0.5, 0.75, 1.0）

    |x| <= 0.1                   -> 0.0（不感帯）
    0.1   < |x| < 0.325          -> 0.25
    0.325 <= |x| < 0.55          -> 0.5
    0.55  <= |x| < 0.775         -> 0.75
    0.775 <= |x| <= 1.0          -> 1.0
    """
    magnitude = abs(stick_x)
    if magnitude <= STEER_DEADZONE:
        quantized = 0.0
    elif magnitude < STEER_THRESHOLD_1:
        quantized = 0.25
    elif magnitude < STEER_THRESHOLD_2:
        quantized = 0.5
    elif magnitude < STEER_THRESHOLD_3:
        quantized = 0.75
    else:
        quantized = 1.0
    return quantized if stick_x >= 0 else -quantized


async def ws_controller_loop():
    """ゲームパッドの入力状態を監視し、変化があった時だけラズパイへ送信するループ"""
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

        last_sent_payload = None

        while running:
            pygame.event.pump()

            a_pressed = bool(joystick.get_button(BUTTON_A)) if joystick else False
            b_pressed = bool(joystick.get_button(BUTTON_B)) if joystick else False
            stick_x = joystick.get_axis(AXIS_LSTICK_X) if joystick else 0.0

            # AボタンとBボタンが同時に押されていたらAボタン（前進）を優先する
            if a_pressed:
                direction = "forward"
            elif b_pressed:
                direction = "backward"
            else:
                direction = "stop"

            payload = {
                "cmd": direction,
                "stick_x": quantize_stick(stick_x),
            }

            # 入力状態に変化がある時だけ送信（サーバー側は次の送信があるまで現状態を維持する）
            if payload != last_sent_payload:
                await ws.send(json.dumps(payload))
                last_sent_payload = payload
                print(f"Sent command: {payload}")

            await asyncio.sleep(SEND_INTERVAL_SEC)

    pygame.quit()


def network_worker():
    """別スレッドでゲームパッド監視とWebSocket通信のイベントループを回す"""
    global running
    try:
        asyncio.run(ws_controller_loop())
    except websockets.exceptions.ConnectionClosed:
        print("Connection closed by server.")
    finally:
        running = False


if __name__ == "__main__":
    # ゲームパッド/WebSocket通信用スレッドの起動
    net_thread = threading.Thread(target=network_worker, daemon=True)
    net_thread.start()

    try:
        # GUI表示はメインスレッドで行う（macOSの制約）
        camera_stream_main()
    except KeyboardInterrupt:
        print("Interrupted by user, exiting.")
    finally:
        running = False
        net_thread.join(timeout=2)
