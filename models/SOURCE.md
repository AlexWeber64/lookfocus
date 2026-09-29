# models/

## face_landmarker.task

MediaPipe Face Landmarker model bundle (face detector + Face Mesh V2 + blendshapes), float16.

| | |
|---|---|
| Source page | https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker#models (now served from developers.google.com/edge/mediapipe/…) |
| Download URL | https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task |
| Downloaded | 2026-09-30 |
| Server Last-Modified | Wed, 03 May 2023 18:02:21 GMT (ETag `b0e7274907a1644404fef66b28dd6d85`) |
| Size | 3,758,596 bytes |
| SHA-256 | `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff` |
| Licence | Apache 2.0 (MediaPipe models); model cards linked from the source page |

The URL points at `latest`, so it could change upstream. The file is committed so that the app never
needs the network, and `tests/test_privacy.py` checks the hash above. To refresh it deliberately:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri <Download URL> -OutFile models\face_landmarker.task
(Get-FileHash models\face_landmarker.task -Algorithm SHA256).Hash.ToLower()
```

then update the hash here and in `src/lookfocus/privacy.py` (`MODEL_SHA256`).
