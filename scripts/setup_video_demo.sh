#!/usr/bin/env bash
# scripts/setup_video_demo.sh
#
# Reproduces the exact repository state used in DEMO_VIDEO_SCRIPT.md,
# so the numbers you see when recording match the script closely.
# Leaves you INSIDE the demo directory, ready to run the first
# command in the script (vault snapshot -m "initial project").
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V="python3 $SCRIPT_DIR/../chronovault.py"
DEMO_DIR="${1:-/tmp/chronovault-video-demo}"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR/src"
cd "$DEMO_DIR"

$V init . > /dev/null

python3 -c "
for i in range(1, 6):
    lines = [f'def function_{i}_{j}(): return {i*100+j}  # module {i}' for j in range(80)]
    open(f'src/module_{i}.py', 'w').write(chr(10).join(lines))
"
cp src/module_1.py src/module_1_backup.py  # real duplicate, for the dedup number

echo "Demo repository ready at: $DEMO_DIR"
echo "cd $DEMO_DIR"
echo ""
echo "Next: vault snapshot -m \"initial project\"   (see DEMO_VIDEO_SCRIPT.md)"
