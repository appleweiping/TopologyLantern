* Clean-room PMOS current-mirror topology for graph and inference examples.
.subckt current_mirror ref out vdd
mref ref ref vdd vdd pch w=2u l=180n
mout out ref vdd vdd pch w=2u l=180n
.ends current_mirror
