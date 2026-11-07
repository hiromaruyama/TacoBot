# Claims

One file per active branch: `<branch with / replaced by __>.toml`, created by `scripts/claims.py claim`.
Because each branch only ever adds **its own** file, claim files never merge-conflict.

Other agents discover your claim by reading this folder **on your remote branch** (`claims.py check` / `sync`),
so push right after claiming. Remove it with `claims.py release` before marking the PR ready.
