# ClipTrimmer
Fast, near-lossless trimmer for NVIDIA Instant Replay clips.

- Per-game filters from your clips folder
- Smooth GPU-decoded preview (mpv), frame stepping, multi-audio-track support
- Smart cut: video is stream-copied, only the first few frames are re-encoded (NVENC), so cuts are frame-accurate with no visible quality loss
- Lossless mode copies the video untouched (exact start on playback); optional Clean cut re-encodes near-lossless
- Exports are verified before anything is replaced; the original goes to the Recycle Bin
- Log file: %APPDATA%\\ClipTrimmer\\cliptrimmer.log
- Save as new (`Game [EDIT] date...`) or replace the original (same name)

## Install
Download `ClipTrimmer-windows.zip` from Releases, extract, run `ClipTrimmer.exe`. No other setup needed.

Keys: Space play/pause, Left/Right frame step, Shift+Left/Right 5s, I/O set start/end.
