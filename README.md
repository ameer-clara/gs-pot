# gs-pot

**Robot-scanned Gaussian Splats → VR walkthroughs in the browser.**

The Unitree Go2 walks an apartment, streams RGB + odometry, we train a 3D
Gaussian Splat from the captured frames, and the buyer/renter views it in any
browser — with WebXR for Quest / Vision Pro / Cardboard.

Built for the [DIMENSIONAL Hackathon Shanghai](https://github.com/grmkris/robohack)
(May 26–28, 2026) as a submodule of
[grmkris/robohack](https://github.com/grmkris/robohack) at `packages/gs-pot`.

## Why
The China real-estate VR-tour market is large and proven (Beike/Lianjia ships
~1M VR-shot apartments). The bottleneck is the human shooter with the rig.
A quadruped is the natural shooter: walks itself, runs every day, no
schedule. This is the *落地* (real-deployment) wedge for the Open/Creative track.

## Status
Day-zero scaffold. See [CLAUDE.md](./CLAUDE.md) for the build plan, pipeline,
and open decisions.
