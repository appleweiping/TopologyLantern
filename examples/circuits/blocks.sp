* Reusable clean-room blocks used by ota.sp.
.subckt diff_pair inp inn outp outn tail vss
mleft outp inp tail vss nch w=4u l=180n
mright outn inn tail vss nch w=4u l=180n
.ends diff_pair

.subckt current_mirror ref out vdd
mref ref ref vdd vdd pch w=2u l=180n
mout out ref vdd vdd pch w=2u l=180n
.ends current_mirror
