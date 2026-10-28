"""Everything ``setup`` installs, pinned in one place, with the sizes ``--check`` reports.

Sizes are static (no network in ``--check``) and rounded up: they are for the disk check and
the confirmation prompt, not for accounting.
"""

from __future__ import annotations

VPIPE_REPO = "https://github.com/tgo-app-dev/vpipe.git"
VPIPE_TAG = "v0.1.80"
# Tags can be moved; the tags we test against are pinned to their commit.
VPIPE_KNOWN_COMMITS = {"v0.1.80": "a90cf05abb92863213adf7259a5f936266c92af2"}
# vpipe's generative stack needs macOS 26 (Metal 4)
MIN_MACOS_MAJOR = 26

# A recursive clone of vpipe plus its CMake build tree
VPIPE_CHECKOUT_BYTES = 1_000_000_000
VPIPE_BUILD_DISK_BYTES = 5_000_000_000

# Download size of each model setup fetches, by RequiredModel.key
MODEL_DOWNLOAD_BYTES = {
    "local/MiniMax-H3-FL2VA-8bit": 117_000_000_000,
    "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema": 1_000_000_000,
    "JunhaoZhuang/FlashVSR-v1.1": 6_800_000_000,
}
# Kept free on top of what a component needs at its peak
DISK_MARGIN_BYTES = 10_000_000_000
