# Face Scanner — AI Photo Scanner & Multi-Person Router

A desktop AI application for scanning large image collections, recognizing faces against reference photos, detecting duplicates, routing photos for multiple people, and safely resuming interrupted scans.

The application is built with Python, Tkinter, InsightFace, OpenCV, SQLite, perceptual hashing, and background worker threads.

> **Platform note:** The application is primarily designed for Windows. Windows-specific completion notifications and MP3 playback are included, while the core scanning components use cross-platform Python libraries.

---

## ✨ Features

### 🧠 AI Face Recognition

- Uses **InsightFace** with the `buffalo_l` model.
- Generates face embeddings and compares them using cosine similarity.
- Supports multiple reference photos for the same person.
- Configurable recognition threshold.
- Automatic threshold calibration is available.
- Optional AMD DirectML acceleration through ONNX Runtime, with CPU fallback.

### 📸 Reference Photo Quality Analyzer

Reference photos are checked before they are used for recognition.

The analyzer evaluates:

- Image resolution
- Face size
- Face position and cropping
- Sharpness / blur
- Brightness
- Contrast
- Dynamic range
- Clipped dark and bright areas
- Face detector confidence
- Head pose — yaw / pitch / roll
- Eye visibility and local image detail
- Compression artifacts
- Multiple simultaneous quality problems

The result is a quality score from **0–100** with a detailed explanation and recommendation.

### 🔍 Image Scanning

The scanner can process common image formats including:

- JPG / JPEG
- PNG
- BMP
- WEBP
- TIFF / TIF
- HEIC / HEIF
- AVIF
- DNG
- CR2
- NEF
- ARW
- RW2

Scans can use multiple worker threads for faster processing.

### 👥 Multi-Person Router

The Router mode can process photos for multiple people at the same time.

- Supports up to **20 people**
- Each person can have multiple reference photos
- Automatically groups references from subfolders
- Selects an output folder for each person
- Scans one common source folder
- Routes recognized photos to the corresponding person's output folder
- Keeps unmatched photos separate
- Tracks duplicates and errors

Example reference structure:

```text
references/
├── Person 1/
│   ├── photo1.jpg
│   ├── photo2.jpg
│   └── photo3.jpg
├── Person 2/
│   ├── photo1.jpg
│   └── photo2.jpg
└── Person 3/
    ├── photo1.jpg
    └── photo2.jpg
```

### ♻️ Duplicate Detection

The application uses perceptual hashing (`pHash`) to identify visually identical or near-identical images.

It can distinguish:

- Exact file duplicates
- Same visual content with different dimensions
- Similar images according to the configured pHash distance
- Duplicate results during scanning

Duplicate handling can be configured from the advanced settings.

### 💾 Resume / Persistent Scan State

Long scans do not have to start from zero after an interruption.

The application stores scan state in JSON files and tracks:

- Processed files
- Matched files
- Non-matched files
- Duplicate files
- Errors
- In-progress files
- Reference configuration
- Threshold
- Worker count
- Output folder
- Scan statistics

Interrupted operations can therefore be resumed using the saved state.

### 🗄️ SQLite Database

Scan and Router results are stored in SQLite databases.

The application uses SQLite optimizations such as:

- WAL mode
- Batched commits
- Busy timeout
- Memory temporary storage
- Configurable cache size
- Memory mapping where supported

Default databases:

```text
scan_results/scan.db
router_results/router.db
```

### ⏯️ Pause / Stop / Resume

During a scan you can:

- Start scanning
- Pause the operation
- Continue the operation
- Stop the scan
- Roll back the last scan
- Undo the last operation

The application also monitors CPU and memory usage and can throttle processing to keep the system responsive.

### 👀 Live Watch

Live Watch monitors the configured source folder and can automatically process new files added after scanning has started.

This allows the application to behave like a continuously running photo scanner instead of requiring a new manual scan for every batch of images.

### 🕓 Scan History

The application keeps operation history and allows previous scan settings to be restored.

History can include:

- Source folder
- Output folder
- Reference photos
- Threshold
- Worker count
- Scan statistics
- Operation state

### 🔎 Review Queue

Ambiguous or uncertain recognition results can be sent to a Review Queue instead of being immediately classified.

This gives the user an opportunity to manually review uncertain matches.

### ↶ Undo & Rollback

The UI provides tools for:

- Undoing the latest operation
- Rolling back the latest scan
- Reviewing uncertain results

### 📊 Real-Time Progress

During scanning the interface displays:

- Processed files
- Remaining files
- Matches
- Review items
- Duplicates
- Errors
- Processing speed
- Elapsed time
- Progress bar

### 📝 Logging

Application logs are written both to the GUI and to:

```text
face_scanner.log
```

Logs include informational messages, warnings, errors, and scan activity.

### 🔔 Completion Notification

When a scan finishes, the application can:

- Play a completion MP3
- Show a Windows notification

Custom completion music can be placed at:

```text
music/music.mp3
```

If it does not exist, the application falls back to:

```text
assets/completion.mp3
```

---

## 🖥️ Interface

The application uses a dark Tkinter interface with:

- Scan controls
- Progress information
- Scan history
- Review Queue
- Undo
- Rollback
- Advanced settings
- Live logging
- Responsive window layout

The interface contains Georgian UI text in several parts of the application.

---

## 🛠️ Requirements

Recommended environment:

- Windows 10 / 11
- Python 3.10+
- 64-bit Python
- At least 8 GB RAM recommended
- More RAM is recommended for very large photo collections

Main Python dependencies:

```text
numpy
opencv-python
psutil
tqdm
Pillow
ImageHash
InsightFace
ONNX Runtime
```

Tkinter is also required. On standard Windows Python installations, Tkinter is normally included.

---

## 📦 Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it on Windows:

```bash
.venv\Scripts\activate
```

### 3. Upgrade pip

```bash
python -m pip install --upgrade pip
```

### 4. Install dependencies

```bash
pip install numpy opencv-python psutil tqdm Pillow ImageHash insightface onnxruntime
```

If you want to use AMD DirectML acceleration and your environment supports it:

```bash
pip install onnxruntime-directml
```

> The application checks available ONNX Runtime providers and uses DirectML when available, otherwise it falls back to CPU.

---

## ▶️ Run

Start the application with:

```bash
python index.py
```

The first startup may take longer because InsightFace needs to initialize the face-analysis model.

---

## 📁 Recommended Project Structure

```text
Face-Scanner/
│
├── index.py
├── README.md
│
├── assets/
│   └── completion.mp3
│
├── music/
│   └── music.mp3
│
├── scan_results/
│   └── scan.db
│
├── router_results/
│   └── router.db
│
├── scan_state/
│   ├── ...
│   └── router/
│       └── ...
│
└── face_scanner.log
```

The application automatically creates required result/state directories when needed.

---

## ⚙️ Default Configuration

Important defaults currently used by the application include:

| Setting | Default |
|---|---:|
| Detection size | `640 × 640` |
| Worker count | `4` |
| Recognition threshold | `46` |
| Minimum threshold | `20` |
| Maximum threshold | `60` |
| Minimum workers | `1` |
| Maximum workers | `20` |
| CPU resource threshold | `85%` |
| Memory resource threshold | `85%` |
| pHash duplicate distance | `4` |
| Duplicate aspect tolerance | `2%` |

These values can be adjusted through the application's advanced settings where supported.

---

## 🔄 Scan Workflow

A typical scan works like this:

```text
Reference Photos
       │
       ▼
Reference Quality Check
       │
       ▼
Face Embeddings
       │
       ▼
Select Source Folder
       │
       ▼
Scan Images
       │
       ├── Face Match ──────► Matched Output
       │
       ├── Uncertain ───────► Review Queue
       │
       ├── Duplicate ───────► Duplicate Result
       │
       ├── No Match ────────► Non-Matched
       │
       └── Error ───────────► Error Result
       │
       ▼
SQLite + JSON State
       │
       ▼
Scan History / Statistics
```

---

## 👤 Multi-Person Router Workflow

```text
Person 1 References ─┐
Person 2 References ─┤
Person 3 References ─┤
...                   ├──► Face Embeddings
Person 20 References ┘
                         │
                         ▼
                    Source Folder
                         │
                         ▼
                  AI Face Matching
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
       Person 1       Person 2       Person N
       Folder         Folder         Folder
```

---

## 🧹 State & Generated Files

The application generates runtime files such as:

```text
scan_results/
router_results/
scan_state/
face_scanner.log
```

These files contain scan results, state information, databases, and logs.

For a clean GitHub repository, it is recommended to exclude runtime-generated files with `.gitignore`.

Example:

```gitignore
# Python
__pycache__/
*.py[cod]
*.pyo

# Virtual environment
.venv/
venv/
env/

# Logs
*.log

# Runtime databases
*.db
*.db-shm
*.db-wal

# Scan state
scan_state/

# Generated results
scan_results/
router_results/

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
```

---

## 🔐 Privacy

Face recognition is performed locally by the application using the installed Python/InsightFace environment.

The application is designed to process local image files and store its scan state/results locally.

No cloud service is required for the core face-scanning workflow.

> Face recognition is sensitive technology. Use it only with appropriate authorization and in accordance with applicable privacy and data-protection laws.

---

## ⚡ Performance

The scanner is designed for large image collections.

Performance-related features include:

- Multiple worker threads
- Adaptive CPU/memory throttling
- Batched SQLite commits
- Persistent database connections
- JSON state batching
- Quick file fingerprints
- Cached face embeddings
- Duplicate indexes
- Resume tracking
- Background processing
- UI updates scheduled separately from worker processing

The number of workers can be adjusted depending on available CPU/RAM resources.

---

## 🧪 Troubleshooting

### `ModuleNotFoundError`

Install the required dependency:

```bash
pip install <package-name>
```

For example:

```bash
pip install opencv-python
pip install psutil
pip install tqdm
pip install Pillow
pip install ImageHash
pip install insightface
pip install onnxruntime
```

### InsightFace / ONNX Runtime issues

Make sure you are using a 64-bit Python installation and that the installed ONNX Runtime package matches your environment.

Check installed providers:

```python
import onnxruntime as ort
print(ort.get_available_providers())
```

### No face detected

Try a reference photo that is:

- High resolution
- Sharp
- Well lit
- Front-facing
- Not heavily compressed
- Not cropped around the face
- Contains only one visible person

The built-in quality checker can help identify problematic reference photos.

### Scan is slow

Try:

- Increasing the worker count
- Using a suitable ONNX Runtime provider
- Using the maximum performance profile
- Avoiding very large numbers of unnecessary reference images
- Running the scanner from a fast SSD

If CPU/RAM usage becomes too high, reduce the worker count.

### JSON state cannot be saved

On Windows, antivirus software or cloud synchronization services can temporarily lock files.

If this happens, consider:

- Moving the project outside a cloud-synchronized folder
- Excluding the `scan_state` directory from antivirus scanning when appropriate
- Closing editors that may have the state file open

The application includes retry and fallback logic for state saving.

---

## 🧰 Development

The main application is currently contained in:

```text
index.py
```

The code uses:

- `threading`
- `queue`
- `sqlite3`
- `dataclasses`
- `pathlib`
- `tkinter`
- `OpenCV`
- `NumPy`
- `InsightFace`
- `ImageHash`
- `psutil`

The architecture separates the major responsibilities into scanning, face processing, state persistence, database management, routing, duplicate detection, history, and GUI operations.

---

## 📜 License

Add your preferred license before publishing the repository.

For example:

```text
MIT License
```

If you choose MIT, add a `LICENSE` file containing the official MIT License text and update this section accordingly.

---

## ⭐ Contributing

Pull requests and improvements are welcome.

Before submitting a change:

1. Test the application with a small photo collection.
2. Test interrupted/resumed scans.
3. Test duplicate handling.
4. Test Router mode when modifying routing logic.
5. Check that the GUI remains responsive during scanning.
6. Avoid committing generated databases, logs, scan state, or personal photos.

---

## ⚠️ Important

This software is intended as a local photo-processing and face-recognition tool.

Recognition results are probabilistic and should not automatically be treated as definitive identity verification. Review uncertain results before making decisions based on them.
