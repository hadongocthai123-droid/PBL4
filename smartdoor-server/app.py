"""SmartDoor AIoT FastAPI server — non-blocking AI pipeline & ESP32-CAM WebSocket streaming."""
import asyncio
import base64
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import config
import database
from ai_engine import AIEngine
from esp32_bridge import ESP32Bridge

engine = AIEngine()
bridge = None
clients = set()

state = {
    "mode": "FACE_ONLY",
    "locked": True,
    "unlock_until": 0.0,
    "last_unlock": 0.0,
    "voice_pending": None,
    "spoof_streak": 0,
    "last_spoof_alert": 0.0,
    "face_threshold": config.FACE_THRESHOLD,
    "spoof_threshold": config.ANTISPOOF_THRESHOLD,
    "voice_threshold": config.VOICE_THRESHOLD,
    "unlock_duration": config.UNLOCK_DURATION,
}


def require_api_key(x_api_key: str = Header(default="")):
    """Chỉ kích hoạt khi cấu hình biến môi trường SMARTDOOR_API_KEY."""
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(401, "Thiếu hoặc sai X-API-Key")


def _load_persisted_thresholds():
    try:
        saved = json.loads(config.THRESHOLDS_SETTINGS_PATH.read_text(encoding="utf-8").lstrip("\ufeff"))
    except (OSError, ValueError, TypeError):
        return
    for key, lo, hi in (
        ("face_threshold", 0.1, 0.99),
        ("spoof_threshold", 0.1, 0.99),
        ("voice_threshold", 0.1, 0.99),
        ("unlock_duration", 1, 30),
    ):
        if key in saved:
            try:
                state[key] = max(lo, min(hi, float(saved[key])))
            except (TypeError, ValueError):
                pass
    config.FACE_THRESHOLD = state["face_threshold"]
    config.ANTISPOOF_THRESHOLD = state["spoof_threshold"]
    config.VOICE_THRESHOLD = state["voice_threshold"]


def _save_persisted_thresholds():
    payload = {k: state[k] for k in ("face_threshold", "spoof_threshold", "voice_threshold", "unlock_duration")}
    try:
        config.THRESHOLDS_SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _sync_members_from_embeddings():
    """Đồng bộ các user_id có sẵn trong face_db.npy / voice_db.npy vào bảng members của SQLite."""
    for uid in engine.list_enrolled_users().keys():
        database.ensure_member(uid)


def _prune_old_captures():
    try:
        files = sorted(config.CAPTURES_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[config.MAX_CAPTURE_FILES:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


@asynccontextmanager
async def lifespan(app):
    global bridge
    database.init_db()
    os.makedirs(config.CAPTURES_DIR, exist_ok=True)
    _load_persisted_thresholds()
    _sync_members_from_embeddings()

    bridge = ESP32Bridge(config.ESP32_CAM_URL)
    try:
        settings = json.loads(config.CAMERA_SETTINGS_PATH.read_text(encoding="utf-8").lstrip("\ufeff"))
        bridge.set_source(settings.get("source", "webcam"), settings.get("url", config.ESP32_CAM_URL))
    except (OSError, ValueError, TypeError):
        pass

    task = asyncio.create_task(camera_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if bridge is not None:
            bridge.running = False


app = FastAPI(title="SmartDoor AIoT", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


async def publish(data):
    for ws in list(clients):
        try:
            await ws.send_json(data)
        except Exception:
            clients.discard(ws)


def _write_capture_file(frame):
    name = f"{int(time.time() * 1000)}.jpg"
    cv2.imwrite(str(config.CAPTURES_DIR / name), frame)
    _prune_old_captures()
    return f"captures/{name}"


async def record_event(result, info=None, frame=None):
    info = info or {}
    image_path = None
    if frame is not None:
        image_path = await asyncio.to_thread(_write_capture_file, frame)

    user_id = info.get("user")
    display_name = info.get("display_name") or (await asyncio.to_thread(database.get_member_name, user_id) if user_id else None)
    await asyncio.to_thread(
        database.add_event,
        result,
        user_id,
        display_name,
        info.get("face_score"),
        info.get("spoof_score"),
        info.get("voice_score"),
        image_path,
    )
    await publish(
        {
            "type": "event",
            "result": result,
            "user_id": user_id,
            "user": display_name or user_id,
            "face_score": info.get("face_score"),
            "spoof_score": info.get("spoof_score"),
            "voice_score": info.get("voice_score"),
            "image_path": image_path,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def crop_detected_face(frame):
    found = engine.detect(frame)
    if not found:
        raise HTTPException(422, "Không phát hiện khuôn mặt trong ảnh")
    x1, y1, x2, y2 = map(int, max(found, key=lambda d: d["confidence"])["box"])
    h, w = frame.shape[:2]
    mx = max(1, (x2 - x1) // 10)
    my = max(1, (y2 - y1) // 10)
    crop = frame[max(0, y1 - my):min(h, y2 + my), max(0, x1 - mx):min(w, x2 + mx)]
    if crop.size == 0:
        raise HTTPException(422, "Vùng khuôn mặt không hợp lệ")
    return crop


def _analyze_and_encode_frame(frame):
    """Chạy toàn bộ suy luận AI (Model 1/2/3) và nén JPEG trong worker thread để không chặn event loop."""
    detections = engine.detect(frame)
    names = database.member_name_map()
    boxes = []
    for det in detections:
        info = engine.inspect_face(frame, det["box"])
        info["box"] = det["box"]
        info["detection_score"] = det["confidence"]
        uid = info["user"]
        display_name = names.get(uid, uid) if uid else None
        info["display_name"] = display_name
        info["label"] = display_name or ("Giả mạo" if info["spoof_score"] is not None and not info["real"] else "Người lạ")
        info["kind"] = "known" if uid and info["real"] else ("spoof" if not info["real"] else "unknown")
        boxes.append(info)

    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    encoded = "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii") if ok else None
    return boxes, encoded


async def unlock(user="remote"):
    now = time.monotonic()
    if now - state["last_unlock"] < config.UNLOCK_COOLDOWN:
        return False
    duration = int(round(state["unlock_duration"]))
    state["last_unlock"] = now
    state["locked"] = False
    state["unlock_until"] = now + duration
    await bridge.send_command({"event": "DOOR_COMMAND", "action": "UNLOCK", "user": user, "duration_sec": duration})
    await publish({"type": "lock", "locked": False, "duration_sec": duration, "user": user})
    return True


async def camera_loop():
    previous_frame_time = time.monotonic()
    access_latched = False
    last_known_face = time.monotonic()
    last_seq = -1

    while True:
        frame, seq = bridge.get_frame(after_seq=last_seq)
        if frame is not None:
            last_seq = seq
            boxes, encoded_img = await asyncio.to_thread(_analyze_and_encode_frame, frame)
            now = time.monotonic()

            known_this_frame = False
            spoof_info = None

            for info in boxes:
                if not info["real"] and engine.antispoof_enabled and spoof_info is None:
                    spoof_info = info
                if info["user"] and info["real"]:
                    known_this_frame = True
                    last_known_face = now
                    if state["mode"] == "FACE_AND_VOICE_2FA":
                        pending = state["voice_pending"]
                        if not pending or pending["user"] != info["user"]:
                            state["voice_pending"] = {
                                "user": info["user"],
                                "display_name": info["display_name"],
                                "until": now + config.VOICE_TIMEOUT,
                                "info": info,
                            }
                            await publish(
                                {
                                    "type": "voice_pending",
                                    "user_id": info["user"],
                                    "user": info["display_name"] or info["user"],
                                    "seconds": config.VOICE_TIMEOUT,
                                }
                            )
                    elif not access_latched and await unlock(info["user"]):
                        await record_event("Mở cửa thành công", info, frame)
                        access_latched = True

            # Đếm spoof theo từng frame thay vì reset giữa các mặt trong cùng 1 frame
            if spoof_info is not None:
                state["spoof_streak"] += 1
                if state["spoof_streak"] >= 3 and (now - state["last_spoof_alert"]) >= config.SPOOF_ALERT_COOLDOWN:
                    state["last_spoof_alert"] = now
                    await bridge.send_command({"event": "ALERT", "type": "SPOOF_DETECTED"})
                    await record_event("Chặn giả mạo", spoof_info, frame)
            else:
                state["spoof_streak"] = 0

            if not known_this_frame and (now - last_known_face) > 1.2:
                access_latched = False
                if state["voice_pending"] is not None:
                    state["voice_pending"] = None
                    await publish({"type": "voice_cleared"})

            if encoded_img is not None:
                fps = round(1.0 / max(0.001, now - previous_frame_time), 1)
                previous_frame_time = now
                await publish(
                    {
                        "type": "frame",
                        "image": encoded_img,
                        "boxes": boxes,
                        "fps": fps,
                        "telemetry": bridge.get_telemetry(),
                        "locked": state["locked"],
                        "source": bridge.source,
                    }
                )

        now = time.monotonic()
        if not state["locked"] and now >= state["unlock_until"]:
            state["locked"] = True
            await bridge.send_command({"event": "DOOR_COMMAND", "action": "LOCK"})
            await publish({"type": "lock", "locked": True})

        pending = state["voice_pending"]
        if pending and now > pending["until"]:
            state["voice_pending"] = None
            await publish({"type": "voice_cleared"})
            await publish({"type": "notice", "message": "Hết thời gian xác thực giọng nói"})

        await asyncio.sleep(0.025)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def status():
    return {
        "models": engine.get_status(),
        "antispoof_enabled": engine.antispoof_enabled,
        "camera_source": bridge.source,
        "camera_url": bridge.url,
        "camera_has_frame": bridge.has_frame(),
        "camera_frame_source": bridge.frame_source(),
        "mode": state["mode"],
        "locked": state["locked"],
        "telemetry": bridge.get_telemetry(),
        "thresholds": {k: state[k] for k in ("face_threshold", "spoof_threshold", "voice_threshold", "unlock_duration")},
    }


@app.get("/api/camera/snapshot.jpg")
async def camera_snapshot():
    """Trả ảnh JPEG mới nhất nhận được từ webcam hoặc ESP32-CAM để kiểm tra nhanh kết nối."""
    frame = bridge.frame()
    if frame is None:
        raise HTTPException(503, "Chưa nhận được frame từ camera")
    ok, jpg = await asyncio.to_thread(cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(500, "Không thể mã hóa ảnh")
    return Response(content=jpg.tobytes(), media_type="image/jpeg")


@app.post("/api/models/reload", dependencies=[Depends(require_api_key)])
async def reload_models():
    models = await asyncio.to_thread(engine.reload)
    await asyncio.to_thread(_sync_members_from_embeddings)
    await publish({"type": "status", "models": models, "locked": state["locked"]})
    return {"models": models, "antispoof_enabled": engine.antispoof_enabled}


@app.post("/api/camera", dependencies=[Depends(require_api_key)])
async def camera(payload: dict):
    try:
        bridge.set_source(payload.get("source", "webcam"), payload.get("url"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    config.CAMERA_SETTINGS_PATH.write_text(
        json.dumps({"source": bridge.source, "url": bridge.url}, ensure_ascii=False),
        encoding="utf-8",
    )
    await publish({"type": "camera_source", "source": bridge.source, "url": bridge.url})
    await publish({"type": "telemetry", "telemetry": bridge.get_telemetry()})
    return {"source": bridge.source, "url": bridge.url, "telemetry": bridge.get_telemetry()}


@app.post("/api/mode", dependencies=[Depends(require_api_key)])
async def mode(payload: dict):
    m = payload.get("mode")
    if m not in ("FACE_ONLY", "FACE_AND_VOICE_2FA"):
        raise HTTPException(400, "Chế độ không hợp lệ")
    state["mode"] = m
    if m == "FACE_ONLY" and state["voice_pending"] is not None:
        state["voice_pending"] = None
        await publish({"type": "voice_cleared"})
    return {"mode": m}


@app.post("/api/thresholds", dependencies=[Depends(require_api_key)])
async def thresholds(payload: dict):
    for key, lo, hi in (
        ("face_threshold", 0.1, 0.99),
        ("spoof_threshold", 0.1, 0.99),
        ("voice_threshold", 0.1, 0.99),
        ("unlock_duration", 1, 30),
    ):
        if key in payload:
            try:
                state[key] = max(lo, min(hi, float(payload[key])))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Giá trị {key} không hợp lệ")
    config.FACE_THRESHOLD = state["face_threshold"]
    config.ANTISPOOF_THRESHOLD = state["spoof_threshold"]
    config.VOICE_THRESHOLD = state["voice_threshold"]
    _save_persisted_thresholds()
    return {k: state[k] for k in ("face_threshold", "spoof_threshold", "voice_threshold", "unlock_duration")}


@app.post("/api/unlock", dependencies=[Depends(require_api_key)])
async def remote_unlock():
    if not await unlock("remote"):
        raise HTTPException(429, "Đang trong thời gian chờ giữa các lần mở khóa")
    await record_event("Mở cửa từ xa")
    return {"ok": True}


@app.post("/api/lock", dependencies=[Depends(require_api_key)])
async def emergency_lock():
    state["locked"] = True
    state["unlock_until"] = 0.0
    await bridge.send_command({"event": "DOOR_COMMAND", "action": "LOCK"})
    await publish({"type": "lock", "locked": True})
    return {"ok": True}


@app.get("/api/events")
async def events(limit: int = 100):
    return await asyncio.to_thread(database.recent_events, limit)


@app.get("/api/members")
async def members():
    rows = await asyncio.to_thread(database.list_members)
    counts = engine.list_enrolled_users()
    for row in rows:
        info = counts.get(row["user_id"], {"face_samples": 0, "voice_samples": 0})
        row["face_samples"] = info["face_samples"]
        row["voice_samples"] = info["voice_samples"]
    return rows


@app.delete("/api/members/{user_id}", dependencies=[Depends(require_api_key)])
async def delete_member(user_id: str):
    await asyncio.to_thread(database.delete_member, user_id)
    await asyncio.to_thread(engine.delete_user, user_id)
    return {"ok": True}


@app.post("/api/enroll/face", dependencies=[Depends(require_api_key)])
async def enroll_face(user_id: str = Form(...), display_name: str = Form(""), image: UploadFile = File(...)):
    uid = user_id.strip()
    if not uid:
        raise HTTPException(400, "Cần mã người dùng")
    raw = await image.read()

    def _run():
        arr = np.frombuffer(raw, np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(400, "Ảnh không hợp lệ")
        crop = crop_detected_face(frame)
        try:
            count = engine.enroll_face(uid, crop)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        database.upsert_member(uid, display_name)
        return count

    count = await asyncio.to_thread(_run)
    return {"ok": True, "user_id": uid, "samples": count}


@app.post("/api/enroll/face/current", dependencies=[Depends(require_api_key)])
async def enroll_face_current(payload: dict):
    uid = str(payload.get("user_id", "")).strip()
    display_name = str(payload.get("display_name", "")).strip()
    if not uid:
        raise HTTPException(400, "Cần mã người dùng")
    frame = bridge.frame()
    if frame is None:
        raise HTTPException(503, "Chưa nhận được frame từ camera")

    def _run():
        crop = crop_detected_face(frame)
        try:
            count = engine.enroll_face(uid, crop)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        database.upsert_member(uid, display_name)
        return count

    count = await asyncio.to_thread(_run)
    return {"ok": True, "user_id": uid, "samples": count}


@app.post("/api/verify-voice")
async def verify_voice(payload: dict):
    pending = state["voice_pending"]
    now = time.monotonic()
    if not pending or now > pending["until"]:
        state["voice_pending"] = None
        await publish({"type": "voice_cleared"})
        raise HTTPException(400, "Chưa có khuôn mặt hợp lệ đang chờ xác thực giọng nói")

    requested_user = payload.get("user_id")
    if requested_user and requested_user != pending["user"]:
        raise HTTPException(403, "Mã người dùng không khớp phiên xác thực khuôn mặt hiện tại")

    user = pending["user"]
    res = await asyncio.to_thread(
        engine.verify_voice,
        payload.get("samples", []),
        int(payload.get("sample_rate", 16000)),
        user,
    )
    if res.get("ok"):
        state["voice_pending"] = None
        await publish({"type": "voice_cleared"})
        if await unlock(user):
            await record_event("Mở cửa thành công", {**pending.get("info", {}), "voice_score": res["score"]})
    else:
        await record_event(
            "Từ chối giọng nói",
            {"user": user, "display_name": pending.get("display_name"), "voice_score": res.get("score")},
        )
    return res


@app.post("/api/enroll/voice", dependencies=[Depends(require_api_key)])
async def enroll_voice(payload: dict):
    uid = str(payload.get("user_id", "")).strip()
    samples = payload.get("samples", [])
    display_name = str(payload.get("display_name", "")).strip()
    if not uid or len(samples) < 3:
        raise HTTPException(400, "Cần mã thành viên và đủ 3 mẫu")
    try:
        count = await asyncio.to_thread(
            engine.enroll_voice, uid, samples, int(payload.get("sample_rate", 16000))
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    await asyncio.to_thread(database.upsert_member, uid, display_name or uid)
    return {"ok": True, "user_id": uid, "samples": count}


@app.websocket("/ws")
async def ws_ui(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        telemetry = bridge.get_telemetry()
        await ws.send_json({"type": "status", "models": engine.get_status(), "locked": state["locked"]})
        await ws.send_json({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})
        await ws.send_json({"type": "camera_source", "source": bridge.source, "url": bridge.url})
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        clients.discard(ws)


def _check_device_token(ws: WebSocket):
    if config.DEVICE_TOKEN and ws.query_params.get("token", "") != config.DEVICE_TOKEN:
        return False
    return True


@app.websocket("/ws/esp32")
async def ws_esp32(ws: WebSocket):
    if not _check_device_token(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    bridge.esp_socket = ws
    bridge.set_source("esp32", clear_frame=False)
    telemetry = bridge.refresh_online_state()
    await publish({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})
    await publish({"type": "camera_source", "source": "esp32", "url": bridge.url})
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw_bytes = message.get("bytes")
            if raw_bytes is not None:
                await asyncio.to_thread(bridge.decode_and_push_jpeg, raw_bytes, "esp32", "websocket")
                continue

            text = message.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            evt = data.get("event")
            if evt in ("HEARTBEAT", "TELEMETRY"):
                telemetry = bridge.update_telemetry(data, device_hint="esp32-cam")
                await publish({"type": "telemetry", "telemetry": telemetry})
            elif evt == "FRAME" and data.get("image"):
                try:
                    raw = base64.b64decode(data["image"])
                except Exception:
                    continue
                await asyncio.to_thread(bridge.decode_and_push_jpeg, raw, "esp32", "websocket")
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if bridge.esp_socket is ws:
            bridge.esp_socket = None
        telemetry = bridge.refresh_online_state()
        await publish({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})


@app.websocket("/ws/esp32-control")
async def ws_esp32_control(ws: WebSocket):
    if not _check_device_token(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    bridge.controller_socket = ws
    telemetry = bridge.refresh_online_state()
    await publish({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            evt = data.get("event")
            if evt in ("HEARTBEAT", "TELEMETRY"):
                telemetry = bridge.update_telemetry(data, device_hint="esp32-s3")
                if "locked" in data and isinstance(data["locked"], bool):
                    state["locked"] = data["locked"]
                    await publish({"type": "lock", "locked": state["locked"]})
                await publish({"type": "telemetry", "telemetry": telemetry})
            elif evt == "ALERT" and data.get("type") == "INTRUSION":
                await record_event("Cảnh báo đột nhập (MC-38)")
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if bridge.controller_socket is ws:
            bridge.controller_socket = None
        telemetry = bridge.refresh_online_state()
        await publish({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})


@app.websocket("/ws/esp32-cam")
async def ws_cam(ws: WebSocket):
    if not _check_device_token(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    bridge.esp_cam_socket = ws
    bridge.set_source("esp32", clear_frame=False)
    telemetry = bridge.refresh_online_state()
    await publish({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})
    await publish({"type": "camera_source", "source": "esp32", "url": bridge.url})
    try:
        while True:
            raw = await ws.receive_bytes()
            await asyncio.to_thread(bridge.decode_and_push_jpeg, raw, "esp32", "websocket")
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if bridge.esp_cam_socket is ws:
            bridge.esp_cam_socket = None
        telemetry = bridge.refresh_online_state()
        await publish({"type": "esp32", "online": telemetry["online"], "telemetry": telemetry})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=config.HOST, port=config.PORT, reload=False)
