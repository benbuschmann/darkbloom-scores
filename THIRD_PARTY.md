# Upstream scoring dependency

The website imports public capacity/pricing parsers, price blending, and default
weights from [Darkbloom Manager](https://github.com/benbuschmann/darkbloom-manager).

- Version: 0.1.7
- Commit: `dd064209f85fd2b701b3ab6b8a6bfff5ead7db27`
- File: `warm_model_manager.py`
- SHA-256: `7140b60ce1b2e84a138dfc940fffcaa53f045bb4848976f926ca0424d080ca9c`
- License: MIT, copyright (c) 2026 benbuschmann. The full notice is reproduced
  in [LICENSE](LICENSE).
- [Pinned source](https://github.com/benbuschmann/darkbloom-manager/blob/dd064209f85fd2b701b3ab6b8a6bfff5ead7db27/warm_model_manager.py)

`fetch_manager.py` downloads this exact unmodified source from GitHub and
checks its hash before installation. Docker builds require access to GitHub;
runtime does not fetch source code. Updating the dependency requires reviewing
the upstream diff, updating both the commit and checksum, and rerunning tests.
Do not edit the downloaded file or substitute a floating `main` dependency.

The service never calls the upstream provider-management entry point. No
Darkbloom CLI, binaries, models, private configs, keys, or provider state are
bundled. This project's MIT license does not grant rights to Darkbloom's
separately licensed services or documentation. This is an independent community
dashboard, not an official Darkbloom service.
