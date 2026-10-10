#!/usr/bin/env bash
# Create a small local Git repository with one failing test, for the PatchPilot
# patch-workspace demo. Local only: no remote, nothing is pushed.
# Usage: demo/setup-demo-repo.sh [PATH]   (default: ~/patchpilot-demo-repo)
set -euo pipefail
repo="${1:-$HOME/patchpilot-demo-repo}"
if [ -e "$repo" ]; then
  echo "refusing: $repo already exists; remove it or pass another path" >&2
  exit 1
fi
mkdir -p "$repo"
cd "$repo"
git init -q -b main
cat > pricing.py <<'PY'
def apply_discount(price: float, percent: float) -> float:
    """Return price reduced by percent (0-100)."""
    return price - percent
PY
cat > test_pricing.py <<'PY'
import unittest

from pricing import apply_discount


class ApplyDiscountTest(unittest.TestCase):
    def test_ten_percent_off_200(self):
        self.assertEqual(apply_discount(200.0, 10), 180.0)

    def test_zero_percent(self):
        self.assertEqual(apply_discount(50.0, 0), 50.0)


if __name__ == "__main__":
    unittest.main()
PY
printf '__pycache__/\n' > .gitignore
git add .gitignore pricing.py test_pricing.py
git -c user.name=patchpilot-demo -c user.email=demo@patchpilot.invalid commit -q -m "Add pricing with discount bug"
echo "$repo"
