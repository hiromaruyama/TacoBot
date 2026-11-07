# Workcell layout (lane: rig)

Left/right as the robot sees them (standing behind the arms, facing the table), matching upstream.

```
            far side
   [plate]                 [filling cup]
        +-----------------+
        |     GRIDDLE     |   [cheese cup]
        +-----------------+
   [tortilla stack]        [spatula rest]
   LEFT follower        RIGHT follower
            (arm bases)
```

- Overhead camera sees the whole griddle and every station. Wrist cameras on each follower.
- Keep stations in the **same place every run**: Dream-1 and ACT learn from what the cameras see.
- Mark positions with tape and add a photo here (`rig/layout.jpg`) once it's fixed.

TODO(rig): measure the griddle height and reach for both arms; record the gripper open/closed angles per arm.
