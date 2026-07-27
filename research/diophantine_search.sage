#!/usr/bin/env sage
from sage.all import *
import itertools, json, sys, time
proof.all(True)
mode = sys.argv[1] if len(sys.argv) > 1 else 'all'

def emit(kind, **kw):
    print(json.dumps({'kind': kind, **kw}, sort_keys=True), flush=True)

def verify(x,y,z):
    x,y,z=ZZ(x),ZZ(y),ZZ(z)
    return z*z+y*y*z+x*x*x-2 == 0

def map_e1(P):
    if P.is_zero(): return None
    X,Y=QQ(P[0]),QQ(P[1]); den=X*(X+2)
    if den==0: return None
    y=(2*Y+3*X+6)/den
    if y.denominator()!=1: return None
    y=ZZ(y); x=1-y
    w=2+3*y-(X+1)*y*y
    if QQ(w).denominator()!=1: return None
    w=ZZ(w)
    if (w-y*y)%2: return None
    z=(w-y*y)//2
    assert verify(x,y,z)
    return x,y,z

def map_e2(P):
    if P.is_zero(): return None
    U,Y=QQ(P[0]),QQ(P[1]); den=(U-232)*(U-528)
    if den==0: return None
    u=4*(37*Y+71*U-235068)/den
    if u.denominator()!=1: return None
    u=ZZ(u); y=u+8; x=3*u-7
    r=(380-U)/148
    w=74+QQ(71,37)*u+r*u*u
    if w.denominator()!=1: return None
    w=ZZ(w)
    if (w-y*y)%2: return None
    z=(w-y*y)//2
    assert verify(x,y,z)
    return x,y,z

def lattice(E, gens, bound, mapper, label):
    rg=range(-bound,bound+1); r=len(gens); start=time.time(); hits={}; tested=0
    mult=[[n*P for n in rg] for P in gens]
    cut=r//2
    left=[]
    for cs in itertools.product(*([rg]*cut)):
        R=E(0)
        for i,c in enumerate(cs): R += mult[i][c+bound]
        left.append((cs,R))
    right=[]
    for cs in itertools.product(*([rg]*(r-cut))):
        R=E(0)
        for j,c in enumerate(cs,start=cut): R += mult[j][c+bound]
        right.append((cs,R))
    emit('lattice_start',label=label,rank=r,bound=bound,left=len(left),right=len(right),gens=[str(P) for P in gens])
    for lc,LP in left:
        for rc,RP in right:
            cs=lc+rc
            if all(c==0 for c in cs): continue
            tested+=1; out=mapper(LP+RP)
            if out:
                key=tuple(map(int,out))
                if key not in hits:
                    hits[key]=cs
                    emit('surface_hit',label=label,coefficients=list(cs),x=str(out[0]),y=str(out[1]),z=str(out[2]),x_digits=len(str(abs(out[0]))))
    emit('lattice_done',label=label,tested=tested,hits=len(hits),seconds=time.time()-start)

def conics():
    E=EllipticCurve([0,0,0,0,-2]); P=E(3,5)
    emit('mordell',rank=int(E.rank(proof=True)),gens=[str(Q) for Q in E.gens(proof=True)])
    for n in range(1,31):
        Q=n*P; k,v=QQ(Q[0]),QQ(Q[1]); w=abs(v); C=-k**6+40*k**3+32
        Cn=Conic(QQ,[3*k*k*w*w,-4*w**3,-C]); t=time.time()
        try:
            ok,ans=Cn.has_rational_point(point=True)
            emit('conic',multiple=n,soluble=bool(ok),witness=str(ans),seconds=time.time()-t)
        except Exception as ex:
            emit('conic_error',multiple=n,error=repr(ex),seconds=time.time()-t)

def e1():
    E=EllipticCurve([0,0,1,-1,6]); t=time.time(); gens=E.gens(proof=True)
    emit('curve',label='E1',rank=int(E.rank(proof=True)),gens=[str(P) for P in gens],torsion=[str(P) for P in E.torsion_points()],seconds=time.time()-t)
    try:
        pts=E.integral_points(both_signs=True)
        emit('integral_points',label='E1',count=len(pts),points=[str(P) for P in pts])
        for P in pts:
            out=map_e1(P)
            if out: emit('integral_surface',label='E1',point=str(P),x=str(out[0]),y=str(out[1]),z=str(out[2]))
    except Exception as ex: emit('integral_error',label='E1',error=repr(ex))
    lattice(E,gens,100,map_e1,'E1')

def e2():
    E=EllipticCurve([0,0,0,-476688,133008912]); t=time.time()
    known=[E(132,8508),E(QQ(23796,121),QQ(9112140,1331)),E(QQ(18369,49),QQ(-906975,343)),E(276,-4740),E(QQ(311724,1369),QQ(-305064180,50653))]
    try:
        gens=E.gens(proof=True); rank=E.rank(proof=True)
        emit('curve',label='E2',rank=int(rank),gens=[str(P) for P in gens],torsion=[str(P) for P in E.torsion_points()],seconds=time.time()-t)
    except Exception as ex:
        emit('curve_error',label='E2',error=repr(ex),seconds=time.time()-t); gens=known
    try:
        pts=E.integral_points(both_signs=True)
        emit('integral_points',label='E2',count=len(pts),points=[str(P) for P in pts])
        for P in pts:
            out=map_e2(P)
            if out: emit('integral_surface',label='E2',point=str(P),x=str(out[0]),y=str(out[1]),z=str(out[2]))
    except Exception as ex: emit('integral_error',label='E2',error=repr(ex))
    lattice(E,known,10,map_e2,'E2-known')
    if len(gens)<=6: lattice(E,gens,8 if len(gens)>=5 else 18,map_e2,'E2-basis')

if mode in ('all','conics'): conics()
if mode in ('all','e1'): e1()
if mode in ('all','e2'): e2()
