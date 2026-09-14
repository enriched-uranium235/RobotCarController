import asyncio
import json
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
# 実機（PCA9685 + main.py の MotorDriver）が使えない環境ではダミーにフォールバックする
# ==========================================
LEFT_MOTOR = 0
RIGHT_MOTOR = 1


def clamp_speed(speed):
    try:
        speed = int(round(float(speed)))
    except (TypeError, ValueError):
        speed = 0
    return max(0, min(100, speed))


class RealMotorController:
    """main.py の MotorDriver（PCA9685.py 経由でモーターを制御）をラップする"""

    def __init__(self, driver):
        self.driver = driver

    def drive(self, direction, left_speed, right_speed):
        left_speed = clamp_speed(left_speed)
        right_speed = clamp_speed(right_speed)
        self.driver.MotorRun(LEFT_MOTOR, direction, left_speed)
        self.driver.MotorRun(RIGHT_MOTOR, direction, right_speed)

    def stop(self):
        self.driver.MotorStop(LEFT_MOTOR)
        self.driver.MotorStop(RIGHT_MOTOR)


class DummyMotorController:
    """実機が無い環境（開発PCなど）向けの動作確認用ダミー"""

    def drive(self, direction, left_speed, right_speed):
        left_speed = clamp_speed(left_speed)
        right_speed = clamp_speed(right_speed)
        print(f"[Motor] {direction.upper()} L={left_speed}% R={right_speed}%")

    def stop(self):
        print("[Motor] STOP")


try:
    from main import MotorDriver
    motor = RealMotorController(MotorDriver())
    print("[Motor] Hardware driver (PCA9685) initialized.")
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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    motor.stop()
    print("Client connected. Motor standby (STOP).")
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            cmd = message.get("cmd", "stop")
            left_speed = message.get("left_speed", 0)
            right_speed = message.get("right_speed", 0)

            if cmd in ("forward", "backward"):
                motor.drive(cmd, left_speed, right_speed)
            else:
                motor.stop()

            await websocket.send_text(json.dumps({"status": f"processed_{cmd}"}))

    except WebSocketDisconnect:
        motor.stop()
        print("Client disconnected. Safety STOP.")

if __name__ == "__main__":
    motor.stop()
    uvicorn.run(app, host="0.0.0.0", port=8000)
