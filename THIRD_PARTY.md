# Third-party software

This repository vendors no third-party source code. It uses the packages below and talks to the
services below over the network. Licenses were read from each package's published metadata
(installed copies, 2026-11-04) except where noted.

## Shipped in the container image

| Component | Version | License | How it is used |
|---|---|---|---|
| [wyoming](https://github.com/OHF-Voice/wyoming) | 1.10.2 | MIT | Python library for the Wyoming protocol; installed from PyPI with pinned hashes |
| [Python](https://www.python.org/) (official `python:3.12.16-slim` Docker image) | 3.12.16 | PSF License; the image's Debian base carries its own per-package licenses | Base image, pinned by digest |

## Used only to run the tests (not shipped)

| Component | Version | License |
|---|---|---|
| pytest | 9.2.0 | MIT |
| pytest-asyncio | 1.4.1.post1 | Apache-2.0 |
| pluggy | 1.7.0 | MIT |
| iniconfig | 2.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| colorama | 0.4.6 | BSD |

## Services this project talks to (not bundled, not modified)

- **faster-whisper** through a Wyoming server such as `wyoming-faster-whisper`.
- **Speech-to-Phrase** (the Home Assistant app, 1.4.x) in compare mode.
- **Home Assistant**, which calls this proxy as a speech-to-text engine.

These run as their own programs under their own licenses; see each project. Nothing from them is
copied into this repository.

If you add vendored code later, keep its license text and record the source commit here.
