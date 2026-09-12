# Workstation measurements

Measurement reference published 2026-09-12 from the workstation team's report.
This page records geometry, a reference initial pose and a short command-timing
test for the UFACTORY xArm6 and Intel RealSense D415 installation. It supplements
the [model interface](WORKSTATION_DATA.md); it does not change the selected
competition's model contract or runtime configuration.

[Download the measurement JSON](data/workstation-measurements-2026-09-12.json).

## Normal gripper TCP

The reported normal-gripper TCP translation is `[0, 0, 172] mm`, or
`[0, 0, 0.172] m`, relative to the flange. This is the gripper configuration
after removing the calibration probe. The probe's extended TCP is not the normal
evaluation gripper TCP. TCP geometry does not change the joint-space action format.

## Reference initial joint pose

The report records the following approximate absolute joint positions, in native
xArm6 J1-to-J6 order. The radian values below are calculated from the report's
rounded degree values; their extra decimal places do not imply greater measurement
precision. This reference does not replace a more precise season runtime reset
configuration.

| Joint | Reported degrees | Converted radians |
|---|---:|---:|
| J1 | -7.4 | -0.129154365 |
| J2 | -5.3 | -0.092502450 |
| J3 | -35.2 | -0.614355897 |
| J4 | 22.9 | 0.399680399 |
| J5 | 40.0 | 0.698131701 |
| J6 | -29.0 | -0.506145483 |

Model state and training use radians. Do not feed degree values directly into a
radian-based model interface.

## Camera to robot base transform

The reported transform maps a point from camera coordinates into robot base
coordinates: `P_base = R_base_camera @ P_camera + t_base_camera`.
The following homogeneous matrix uses **meters** for its translation column:

```text
T_base_camera =
[[-0.731918951,  0.380496137, -0.565258647,  0.802734100],
 [ 0.055828010,  0.860262481,  0.506785652, -0.685059506],
 [ 0.679100789,  0.339368758, -0.650884755,  0.884938192],
 [ 0.000000000,  0.000000000,  0.000000000,  1.000000000]]
```

The JSON retains the report's numeric precision and the quaternion in `xyzw`
order. This calibration applies only while the camera and robot base remain
mechanically fixed in the measured arrangement. Recalibrate after changing their
relative mounting; do not copy this transform onto a differently mounted rig.

| Calibration measurement | Result |
|---|---|
| Fit points | 10 |
| Fit RMS residual | 3.048 mm |
| Maximum fit residual | 4.323 mm |
| Accepted independent validation points | 2 |
| Accepted validation errors | 0.991 mm and 5.855 mm |
| Excluded validation point | 1, with error 8.244 mm |

These are measured sample errors, not a guaranteed accuracy bound across the
entire workspace. The excluded validation point is retained in this summary so
the accepted-point results are not presented as the complete validation sample.

## Short command timing test

On 2026-09-11, one 50 Hz test sent 50 joint commands over one second. J6 performed
a `sin^3` out-and-back trajectory with an excursion of +/-0.1 degrees. Other
joints remained at their measured starting positions; no gripper commands were
sent.

| Command-send measurement | Result |
|---|---:|
| Measured command rate | 50.000389 Hz |
| Mean command period | 19.999844 ms |
| P95 command period | 20.020892 ms |
| P99 command period | 20.040208 ms |
| Maximum command period | 20.045645 ms |
| P95 SDK call duration | 0.330204 ms |
| P99 SDK call duration | 0.356543 ms |
| Maximum SDK call duration | 0.366636 ms |
| Deadline misses greater than 2 ms | 0 |

These measurements describe command-send timing under this short, small-amplitude
test. They are not model inference frequency or camera FPS and do not establish
long-duration performance. They do not redefine the model prediction horizon,
execution prefix or competition runtime configuration.
