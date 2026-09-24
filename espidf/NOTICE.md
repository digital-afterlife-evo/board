# Attribution and license

This ESP-IDF port is distributed under GPL-3.0-only; see LICENSE.

The KX-R60 transfer sequence is adapted from Matthew Nielsen (xunker),
[panasonic_typewriter_interface](https://github.com/xunker/panasonic_typewriter_interface),
commit `f0dacdc7e1627dce5a4d6cc75f59393ccea50063` (GPL-3.0).
Relevant sources: `panasonic_typewriter_interface.ino` (`sendByte`, pin setters,
`waitForACKToGo`, `processByte`, `relayLoop`), `README.md`, `MODELS.md`,
`PINOUT.md`, `rp-k10x_vs_kx-r60.md`, and the optional helpers under `src/`.

Changes in this port (2026-09-22): native ESP-IDF GPIO/console, ESP32-S3 pin
mapping, finite ACK deadlines, idle validation, latched faults, explicit recovery,
released outputs after success/error, and no Arduino/AVR dependencies.
Only the KX-R60 Mini-DIN-8 path is implemented. No extended-character translation.

The upstream checkout in `upstream/` is a local reference and is not built or
committed. When distributing this port's firmware, retain notices and provide
the corresponding source and build instructions as required by GPL-3.0.
