.subckt leaf p n PARAMS: ratio=1
r1 p n 1k
.ends leaf
.subckt top p n
xleaf p n leaf PARAMS: undeclared=2
.ends top
.end
