# 🚀 Face Finder PRO

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![AI Engine](https://img.shields.io/badge/Powered%20by-InsightFace-orange.svg)](https://github.com/deepinsight/insightface)

**Face Finder PRO** is an advanced **AI-powered face recognition and photo organizer** built in Python.  
It scans any folder of images, detects faces, compares them with one or more reference photos,  
and automatically **copies all matching photos** into your chosen output folder — fast, accurate, and simple to use.

---

## ✨ Features

- 🧠 **AI Face Recognition** – Uses `insightface` (Buffalo_L model) for accurate embeddings  
- 👥 **Multiple People Supported** – Match unlimited reference faces at once  
- ⚙️ **Multithreaded Engine** – Processes multiple images simultaneously  
- ⚡ **Performance Stats** – Real-time progress, speed (img/s), ETA  
- 💾 **Database Logging** – Saves results into `results.db` (SQLite)  
- 🧩 **Duplicate Protection** – Skips already-processed photos via perceptual hash  
- 🧱 **GPU Acceleration** – Automatic AMD DirectML or CPU fallback  
- 🪟 **Simple GUI** – Tkinter interface: select, scan, and save  
- 🔒 **Resource-Safe** – Auto-pauses if CPU/RAM usage exceeds 85%

---

## 📸 Screenshots

> *(Add your own screenshots here!)*  
> Example:
> ```text
> Face Finder PRO GUI Interface
> [📷 your_screenshot_here.png]
> ```

---

## 🧰 Installation

### 1️⃣ Clone the Repository
```bash
git clone https://github.com/yourusername/FaceFinderPRO.git
cd FaceFinderPRO
