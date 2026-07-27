#!/usr/bin/env sage
from sage.all import *
import itertools, json, sys, time
proof.all(True)
n = Integer(sys.argv[1]) if len(sys.argv)>1 else Integer(1)

def emit(kind, **kw): print(json.dumps({'kind':kind, **kw}, sort_keys=True), flush=True)
def verify(x,y,z): return z*z+y*y*z+x*x*x-2 == 0

E0=EllipticCurve([0,0,0,0,2]); P=E0(-1,1); Q=n*P
k,v=QQ(Q[0]),QQ(Q[1])
s0=3*(k**4+8*k)/(v**2)
d0=(k**4-16*k)/(v**2)
r0=k**2/v
nq=(k**6+40*k**3-32)/(v**3)
E=EllipticCurve([0,0,0,s0,0])
emit('family',multiple=int(n),k=str(k),v=str(v),s0=str(s0),d0=str(d0),curve=str(E))

def to_surface(R):
    if R.is_zero(): return None
    T,W=QQ(R[0]),QQ(R[1])
    x=(T*T-d0)/4; y=W/2
    U=(T**3-3*r0*T*T+s0*T-nq)/8
    z=-U
    if any(q.denominator()!=1 for q in (x,y,z)): return None
    x,y,z=ZZ(x),ZZ(y),ZZ(z)
    assert verify(x,y,z)
    return x,y,z

t=time.time()
try:
    rank=E.rank(proof=True); gens=E.gens(proof=True); tors=E.torsion_points()
    emit('curve_data',multiple=int(n),rank=int(rank),gens=[str(R) for R in gens],torsion=[str(R) for R in tors],seconds=time.time()-t)
except Exception as ex:
    emit('curve_error',multiple=int(n),error=repr(ex),seconds=time.time()-t); sys.exit(0)

try:
    pts=E.integral_points(both_signs=True)
    emit('integral_points',multiple=int(n),count=len(pts),points=[str(R) for R in pts])
    for R in pts:
        out=to_surface(R)
        if out: emit('surface_hit',multiple=int(n),source='integral_points',point=str(R),x=str(out[0]),y=str(out[1]),z=str(out[2]),x_digits=len(str(abs(out[0]))))
except Exception as ex: emit('integral_error',multiple=int(n),error=repr(ex))

rank=len(gens)
bound=60 if rank<=2 else (18 if rank==3 else (7 if rank==4 else 3))
rg=range(-bound,bound+1); mult=[[c*G for c in rg] for G in gens]
tors=E.torsion_points(); tested=0; hits={}; start=time.time()
emit('lattice_start',multiple=int(n),rank=rank,bound=bound,total=(2*bound+1)**rank*len(tors))
for cs in itertools.product(*([rg]*rank)):
    R=E(0)
    for i,c in enumerate(cs): R += mult[i][c+bound]
    for Tors in tors:
        S=R+Tors; tested+=1; out=to_surface(S)
        if out:
            key=tuple(map(int,out))
            if key not in hits:
                hits[key]=(cs,str(Tors))
                emit('surface_hit',multiple=int(n),source='lattice',coefficients=list(cs),torsion=str(Tors),x=str(out[0]),y=str(out[1]),z=str(out[2]),x_digits=len(str(abs(out[0]))))
emit('lattice_done',multiple=int(n),tested=tested,hits=len(hits),seconds=time.time()-start)
