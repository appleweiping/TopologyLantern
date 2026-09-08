* Synthetic coverage of accepted R, C, I, V, D, Q, and M records.
.subckt device_families p n control
rload p n 10k
cbypass p n 2p
ibias p n 20u
vsense p n 0
dclamp p n diode_placeholder area=1
qbuffer p control n npn_placeholder area=2
mswitch p control n n nch_placeholder w=2u l=180n
.ends device_families
.end
