* Clean-room hierarchical single-stage OTA connectivity example.
.include blocks.sp
.subckt ota inp inn out vdd vss
xdiff inp inn nleft nright tail vss diff_pair
xload nleft out vdd current_mirror
itail tail vss 20u
cc out nleft 1p
.ends ota
