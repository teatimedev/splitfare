#!/usr/bin/env sage
from sage.all import *
import itertools, json, sys, time
proof.all(True)
code=sys.argv[1] if len(sys.argv)>1 else '2'
values={
 '2':QQ(2),'3':QQ(3),'5_3':QQ(5,3),'34_15':QQ(34,15),
 '25_41':QQ(25,41),'58_101':QQ(58,101),'87_101':QQ(87,101),
 '9129_3943':QQ(9129,3943),'4250453_7007642':QQ(4250453,7007642)
}
lam=values[code]

def emit(kind,**kw): print(json.dumps({'kind':kind,**kw},sort_keys=True),flush=True)
def verify(x,y,z): return z*z+y*y*z+x*x*x-2==0
A=lam+1/lam; B=3*(1/lam-lam); D=71*(lam-1/lam)
a=A/8; b=B/8; c=-21*A/8; d=D/8
# X=a*T and W=a*y gives W^2=X^3+b*X^2+a*c*X+a^2*d.
E=EllipticCurve([0,b,0,a*c,a*a*d])
emit('family',code=code,lam=str(lam),curve=str(E),ainvs=[str(q) for q in E.ainvs()])

def to_surface(P):
 if P.is_zero(): return None
 X,W=QQ(P[0]),QQ(P[1]); T=X/a; y=W/a
 x=(T*T-17)/4
 N1=(T**3-3*T**2-21*T+71)/8
 z=-lam*N1
 if any(q.denominator()!=1 for q in (x,y,z)): return None
 x,y,z=ZZ(x),ZZ(y),ZZ(z); assert verify(x,y,z); return x,y,z,T

t=time.time()
try:
 rank=E.rank(proof=True); gens=E.gens(proof=True); tors=E.torsion_points()
 emit('curve_data',code=code,rank=int(rank),gens=[str(P) for P in gens],torsion=[str(P) for P in tors],seconds=time.time()-t)
except Exception as ex:
 emit('curve_error',code=code,error=repr(ex),seconds=time.time()-t)
 try:
  rank=E.rank(proof=False); gens=E.gens(proof=False); tors=E.torsion_points()
  emit('curve_data_unproved',code=code,rank=int(rank),gens=[str(P) for P in gens],torsion=[str(P) for P in tors])
 except Exception as ex2: emit('curve_error_unproved',code=code,error=repr(ex2)); sys.exit(0)

try:
 Em=E.global_minimal_model(); iso=E.isomorphism_to(Em)
 emit('minimal',code=code,curve=str(Em),ainvs=[str(q) for q in Em.ainvs()])
 pts=Em.integral_points(both_signs=True)
 emit('minimal_integral_points',code=code,count=len(pts),points=[str(P) for P in pts])
 for Q in pts:
  P=iso.inverse()(Q); out=to_surface(P)
  if out: emit('surface_hit',code=code,source='minimal_integral',point=str(P),x=str(out[0]),y=str(out[1]),z=str(out[2]),T=str(out[3]),x_digits=len(str(abs(out[0]))))
except Exception as ex: emit('integral_error',code=code,error=repr(ex))

# Modest exact Mordell-Weil lattice scan, with every torsion coset.
r=len(gens); bound=100 if r==1 else (25 if r==2 else (8 if r==3 else 3))
rg=range(-bound,bound+1); mult=[[n*P for n in rg] for P in gens]; hits={}; tested=0; start=time.time()
emit('lattice_start',code=code,rank=r,bound=bound,total=(2*bound+1)**r*len(tors))
try:
 for cs in itertools.product(*([rg]*r)):
  R=E(0)
  for i,n in enumerate(cs): R += mult[i][n+bound]
  for Tor in tors:
   tested+=1; out=to_surface(R+Tor)
   if out:
    key=(int(out[0]),int(out[1]),int(out[2]))
    if key not in hits:
     hits[key]=(cs,str(Tor)); emit('surface_hit',code=code,source='lattice',coefficients=list(cs),torsion=str(Tor),x=str(out[0]),y=str(out[1]),z=str(out[2]),T=str(out[3]),x_digits=len(str(abs(out[0]))))
 emit('lattice_done',code=code,tested=tested,hits=len(hits),seconds=time.time()-start)
except Exception as ex: emit('lattice_error',code=code,tested=tested,error=repr(ex),seconds=time.time()-start)
