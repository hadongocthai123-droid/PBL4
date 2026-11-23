"""ONNX inference pipeline with thread-safe hot reload and torchaudio-matched Log-Mel extraction."""
import os
import threading

import cv2
import numpy as np

import config

try:
    import onnxruntime as ort
except ImportError:
    ort = None


def _build_hann_window(win_length=400, n_fft=512):
    """Tạo cửa sổ Hann định kỳ dài win_length rồi zero-pad đối xứng lên n_fft (khớp torch.stft)."""
    n = np.arange(win_length, dtype=np.float32)
    win = 0.5 - 0.5 * np.cos(2.0 * np.pi * n / float(win_length))
    pad_left = (n_fft - win_length) // 2
    pad_right = n_fft - win_length - pad_left
    return np.pad(win, (pad_left, pad_right), mode="constant").astype(np.float32)


def _build_htk_mel_filterbank(sample_rate=16000, n_fft=512, n_mels=64, f_min=0.0, f_max=None):
    """Tạo ma trận Mel filterbank chuẩn HTK (khớp mặc định của torchaudio.transforms.MelSpectrogram).

    Lưu ý: chia đều các điểm trên thang Mel rồi chuyển ngược về đơn vị Hz trước khi lập tam giác lọc
    trên trục tần số Hz (tránh lỗi trộn lẫn đơn vị Hz và Mel).
    """
    if f_max is None:
        f_max = sample_rate / 2.0
    n_freqs = n_fft // 2 + 1
    all_freqs = np.linspace(0.0, sample_rate / 2.0, n_freqs, dtype=np.float64)

    mel_min = 2595.0 * np.log10(1.0 + f_min / 700.0)
    mel_max = 2595.0 * np.log10(1.0 + f_max / 700.0)
    m_pts = np.linspace(mel_min, mel_max, n_mels + 2, dtype=np.float64)
    f_pts = 700.0 * (10.0 ** (m_pts / 2595.0) - 1.0)  # Đổi ngược từ Mel -> Hz

    banks = np.zeros((n_mels, n_freqs), dtype=np.float32)
    for m in range(n_mels):
        left, center, right = f_pts[m], f_pts[m + 1], f_pts[m + 2]
        down = (all_freqs - left) / max(center - left, 1e-12)
        up = (right - all_freqs) / max(right - center, 1e-12)
        banks[m] = np.maximum(0.0, np.minimum(down, up)).astype(np.float32)
    return banks


class AIEngine:
    def __init__(self, load_models=True):
        self.lock = threading.RLock()
        self.sessions = {}
        self.face_db = {}
        self.voice_db = {}
        self.status = {}
        self.antispoof_enabled = False
        self._hann_window = _build_hann_window(win_length=400, n_fft=512)
        self._mel_banks = _build_htk_mel_filterbank(sample_rate=16000, n_fft=512, n_mels=64)
        if load_models:
            self.reload()

    def _session(self, name, path, required=False):
        if not ort or not os.path.exists(path):
            if required:
                self.status[name] = "Thiếu model hoặc onnxruntime"
            else:
                self.status[name] = "Chưa nạp (Bypass)"
            return None
        try:
            sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            self.status[name] = "Sẵn sàng"
            return sess
        except Exception as exc:
            self.status[name] = f"Lỗi nạp: {exc}"
            return None

    def reload(self):
        with self.lock:
            self.sessions = {
                "face": self._session("Model 1", config.MODEL1_PATH, True),
                "recognition": self._session("Model 2", config.MODEL2_PATH, True),
                "antispoof": self._session("Model 3", config.MODEL3_PATH, False),
                "voice": self._session("Model 4", config.MODEL4_PATH, False),
            }
            self.antispoof_enabled = self.sessions["antispoof"] is not None
            self.face_db = self._load_db(config.FACE_DB_PATH)
            self.voice_db = self._load_db(config.VOICE_DB_PATH, create=True)
            return self.status.copy()

    def get_status(self):
        with self.lock:
            return self.status.copy()

    def list_enrolled_users(self):
        """Trả về dict {user_id: {'face_samples': int, 'voice_samples': int}}."""
        with self.lock:
            users = {}
            for uid, arr in self.face_db.items():
                mat = np.asarray(arr)
                count = int(mat.shape[0]) if mat.ndim >= 2 else (1 if mat.size else 0)
                users[uid] = {"face_samples": count, "voice_samples": 0}
            for uid, arr in self.voice_db.items():
                mat = np.asarray(arr)
                count = int(mat.shape[0]) if mat.ndim >= 2 else (1 if mat.size else 0)
                users.setdefault(uid, {"face_samples": 0, "voice_samples": 0})
                users[uid]["voice_samples"] = count
            return users

    def _load_db(self, path, create=False):
        try:
            if os.path.exists(path):
                value = np.load(path, allow_pickle=True).item()
                return value if isinstance(value, dict) else {}
        except Exception:
            pass
        if create:
            try:
                self._save_db_atomic(path, {})
            except OSError:
                pass
        return {}

    @staticmethod
    def _save_db_atomic(path, db):
        path_str = str(path)
        parent = os.path.dirname(path_str)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp_path = path_str + ".tmp.npy"
        np.save(temp_path, db, allow_pickle=True)
        os.replace(temp_path, path_str)

    @staticmethod
    def _blob(img, size, mean=None, std=None):
        img = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), size).astype(np.float32) / 255.0
        if mean is not None:
            img = (img - np.asarray(mean, np.float32)) / np.asarray(std, np.float32)
        return np.transpose(img, (2, 0, 1))[None, ...].astype(np.float32)

    @staticmethod
    def _iou(a, b):
        x1, y1 = max(a[0], b[0]), max(a[1], b[1])
        x2, y2 = min(a[2], b[2]), min(a[3], b[3])
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
        area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
        return inter / max(1.0, area_a + area_b - inter)

    def detect(self, frame):
        with self.lock:
            sess = self.sessions.get("face")
        if sess is None or frame is None or frame.size == 0:
            return []
        h, w = frame.shape[:2]
        x = self._blob(frame, (320, 320))
        try:
            out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
        except Exception:
            return []

        arr = np.asarray(out)
        if arr.ndim == 3:
            arr = arr[0]
        if arr.ndim != 2:
            return []
        # YOLOv8 raw output thường là (channels=5, N=2100) -> chuyển vị sang (N, 5)
        if arr.shape[0] <= 128 and arr.shape[1] > arr.shape[0]:
            arr = arr.T

        candidates = []
        for row in arr:
            if len(row) < 5:
                continue
            if len(row) == 6:
                a0, a1, a2, a3, score, _ = row[:6]
                if score < config.DETECTION_THRESHOLD:
                    continue
                if max(abs(a0), abs(a1), abs(a2), abs(a3)) <= 2.0:
                    a0, a1, a2, a3 = a0 * 320, a1 * 320, a2 * 320, a3 * 320
                # Phân biệt [x1, y1, x2, y2, score, cls] (NMS export) với [cx, cy, w, h, ...]
                if a2 > a0 and a3 > a1:
                    bx1, by1, bx2, by2 = a0, a1, a2, a3
                else:
                    bx1, by1, bx2, by2 = a0 - a2 / 2, a1 - a3 / 2, a0 + a2 / 2, a1 + a3 / 2
            else:
                cx, cy, bw, bh = row[:4]
                score = float(np.max(row[4:]))
                if score < config.DETECTION_THRESHOLD:
                    continue
                if max(abs(cx), abs(cy), abs(bw), abs(bh)) <= 2.0:
                    cx *= 320
                    cy *= 320
                    bw *= 320
                    bh *= 320
                bx1, by1, bx2, by2 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2

            x1 = max(0.0, min(float(w - 1), float(bx1 * w / 320.0)))
            y1 = max(0.0, min(float(h - 1), float(by1 * h / 320.0)))
            x2 = max(0.0, min(float(w - 1), float(bx2 * w / 320.0)))
            y2 = max(0.0, min(float(h - 1), float(by2 * h / 320.0)))
            if x2 - x1 < 4.0 or y2 - y1 < 4.0:
                continue
            candidates.append(([x1, y1, x2, y2], float(score)))

        kept = []
        for box, score in sorted(candidates, key=lambda z: z[1], reverse=True):
            if all(self._iou(box, b) < config.NMS_THRESHOLD for b, _ in kept):
                kept.append((box, score))
        return [{"box": b, "confidence": s} for b, s in kept]

    def _embedding(self, name, image, size, mean=None, std=None):
        with self.lock:
            sess = self.sessions.get(name)
        if not sess:
            return None
        try:
            inp = sess.get_inputs()[0].name
            result = sess.run(None, {inp: self._blob(image, size, mean, std)})[0].reshape(-1).astype(np.float32)
            norm = float(np.linalg.norm(result))
            return result / norm if norm > 1e-9 else result
        except Exception:
            return None

    @staticmethod
    def _match(embedding, db_snapshot):
        if embedding is None or not db_snapshot:
            return None, 0.0
        best, best_score = None, -1.0
        for user, samples in db_snapshot.items():
            try:
                matrix = np.asarray(samples, dtype=np.float32).reshape(-1, embedding.size)
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                matrix = matrix / np.maximum(norms, 1e-9)
                score = float(np.max(matrix @ embedding))
                if score > best_score:
                    best, best_score = user, score
            except Exception:
                continue
        return best, max(best_score, 0.0) if best is not None else 0.0

    def inspect_face(self, frame, box):
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in box]
        mx, my = max(1, (x2 - x1) // 10), max(1, (y2 - y1) // 10)
        crop = frame[max(0, y1 - my):min(h, y2 + my), max(0, x1 - mx):min(w, x2 + mx)]
        if crop.size == 0:
            return {"user": None, "face_score": 0.0, "spoof_score": None, "real": True}

        with self.lock:
            antispoof_sess = self.sessions.get("antispoof")
            face_db_snapshot = dict(self.face_db)

        spoof = None
        real = True
        if antispoof_sess is not None:
            try:
                blob = self._blob(crop, (224, 224), [0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
                z = antispoof_sess.run(None, {antispoof_sess.get_inputs()[0].name: blob})[0].reshape(-1)
                spoof = float(z[1])
                real = spoof >= config.ANTISPOOF_THRESHOLD
            except Exception:
                real = False

        emb = self._embedding("recognition", crop, (112, 112), [0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        user, score = self._match(emb, face_db_snapshot)
        return {
            "user": user if score >= config.FACE_THRESHOLD else None,
            "face_score": score,
            "spoof_score": spoof,
            "real": real,
        }

    def extract_log_mel(self, samples, sample_rate):
        """Trích xuất Log-Mel Spectrogram shape (64, 301) khớp chính xác pipeline huấn luyện Model 4."""
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not audio.size:
            raise ValueError("Mẫu âm thanh trống")
        if int(sample_rate) <= 0:
            raise ValueError("Tần số lấy mẫu không hợp lệ")

        if int(sample_rate) != 16000:
            from scipy.signal import resample_poly
            audio = resample_poly(audio, 16000, int(sample_rate)).astype(np.float32)

        peak = float(np.max(np.abs(audio)))
        if peak > 1e-4:
            audio = audio * (0.9 / peak)

        target_len = 48000  # 3 giây @ 16 kHz
        if len(audio) < target_len:
            repeats = int(np.ceil(target_len / max(1, len(audio))))
            audio = np.tile(audio, repeats)[:target_len]
        elif len(audio) > target_len:
            start = (len(audio) - target_len) // 2
            audio = audio[start:start + target_len]

        # Reflect padding 256 mẫu hai đầu khớp torch.stft(..., n_fft=512, hop_length=160, center=True)
        padded = np.pad(audio, (256, 256), mode="reflect")
        frames = np.lib.stride_tricks.sliding_window_view(padded, 512)[::160]
        windowed = frames * self._hann_window
        spec = np.abs(np.fft.rfft(windowed, axis=1)) ** 2  # (301, 257)
        mel_spec = spec @ self._mel_banks.T                 # (301, 64)
        feat = np.log(mel_spec + 1e-6).T.astype(np.float32) # (64, 301)
        feat = (feat - float(feat.mean())) / (float(feat.std()) + 1e-6)
        if feat.shape != (64, 301):
            raise ValueError(f"Log-Mel sai shape {feat.shape}; cần (64, 301)")
        return feat

    def _voice_embedding(self, samples, sample_rate):
        with self.lock:
            sess = self.sessions.get("voice")
        if not sess:
            raise RuntimeError("Model 4 chưa sẵn sàng")
        feat = self.extract_log_mel(samples, sample_rate)
        inp = next((i.name for i in sess.get_inputs() if i.name == "mel_input"), sess.get_inputs()[0].name)
        emb = sess.run(None, {inp: feat[None, None].astype(np.float32)})[0].reshape(-1).astype(np.float32)
        return emb / max(float(np.linalg.norm(emb)), 1e-9)

    def verify_voice(self, samples, sample_rate, user_id):
        try:
            emb = self._voice_embedding(samples, sample_rate)
            with self.lock:
                entry = self.voice_db.get(user_id)
                db_snapshot = {user_id: entry} if entry is not None else {}
            if not db_snapshot:
                return {"ok": False, "score": 0.0, "error": "Thành viên chưa đăng ký mẫu giọng nói"}
            _, score = self._match(emb, db_snapshot)
            return {"ok": score >= config.VOICE_THRESHOLD, "score": score}
        except Exception as exc:
            return {"ok": False, "score": 0.0, "error": str(exc)}

    def enroll_face(self, user_id, image):
        emb = self._embedding("recognition", image, (112, 112), [0.5] * 3, [0.5] * 3)
        if emb is None:
            raise RuntimeError("Model 2 chưa sẵn sàng")
        with self.lock:
            previous = self.face_db.get(user_id)
            if previous is None:
                updated = emb.reshape(1, -1)
            else:
                old = np.asarray(previous, dtype=np.float32)
                if old.size == 0:
                    old = emb.reshape(0, -1)
                elif old.ndim == 1:
                    old = old.reshape(1, -1)
                if old.shape[1] != emb.size:
                    raise RuntimeError(f"Embedding cũ của {user_id} có chiều {old.shape[1]}, model trả {emb.size}")
                updated = np.concatenate((old, emb.reshape(1, -1)), axis=0)
            self.face_db[user_id] = updated
            self._save_db_atomic(config.FACE_DB_PATH, self.face_db)
            return int(updated.shape[0])

    def enroll_voice(self, user_id, samples, sample_rate):
        if len(samples) < 3:
            raise ValueError("Cần đủ 3 mẫu giọng nói")
        embeddings = np.stack([self._voice_embedding(sample, sample_rate) for sample in samples])
        mean_emb = np.mean(embeddings, axis=0)
        mean_emb /= max(float(np.linalg.norm(mean_emb)), 1e-9)
        with self.lock:
            old = self.voice_db.get(user_id)
            if old is None:
                updated = mean_emb.reshape(1, -1)
            else:
                old_arr = np.asarray(old, dtype=np.float32).reshape(-1, mean_emb.size)
                updated = np.concatenate((old_arr, mean_emb.reshape(1, -1)), axis=0)
            self.voice_db[user_id] = updated
            self._save_db_atomic(config.VOICE_DB_PATH, self.voice_db)
            return int(updated.shape[0])

    def delete_user(self, user_id):
        """Xóa atomic và thread-safe embedding của user_id khỏi cả face_db và voice_db."""
        with self.lock:
            removed_face = self.face_db.pop(user_id, None) is not None
            removed_voice = self.voice_db.pop(user_id, None) is not None
            if removed_face:
                try:
                    self._save_db_atomic(config.FACE_DB_PATH, self.face_db)
                except OSError:
                    pass
            if removed_voice:
                try:
                    self._save_db_atomic(config.VOICE_DB_PATH, self.voice_db)
                except OSError:
                    pass
            return {"face_removed": removed_face, "voice_removed": removed_voice}
