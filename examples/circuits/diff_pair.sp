* Clean-room NMOS differential-pair topology for graph and inference examples.
.subckt diff_pair inp inn outp outn tail vss
mleft outp inp tail vss nch w=4u l=180n
mright outn inn tail vss nch w=4u l=180n
.ends diff_pair
