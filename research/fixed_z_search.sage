#!/usr/bin/env sage
from sage.all import *
import itertools, json, sys, time

proof.all(False)
start = ZZ(sys.argv[1])
end = ZZ(sys.argv[2])


def emit(kind, **payload):
    print(json.dumps({"kind": kind, **payload}, sort_keys=True), flush=True)


def verify(x, y, z):
    return z*z + y*y*z + x*x*x - 2 == 0


def local_obstruction(d):
    # Every solution has a companion with negative z, so only d=-z>0 is needed.
    if d < 0:
        return -1
    fd = factor(d)
    # x^3=2 modulo 4 or 9 is impossible.
    for p, e in fd:
        p, e = ZZ(p), ZZ(e)
        if p == 2 and e >= 2:
            return 4
        if p == 3 and e >= 2:
            return 9
        if p in (2, 3) or p % 3 == 2:
            continue
        if power_mod(2, (p-1)//3, p) != 1:
            return p
    return None


for d in range(start, end + 1):
    if d == 0:
        continue
    obstruction = local_obstruction(d)
    if obstruction is not None:
        emit("local_skip", d=int(d), obstruction=int(obstruction))
        continue

    t0 = time.time()
    c = d**3 * (d**2 - 2)
    E = EllipticCurve([0, 0, 0, 0, c])
    emit("curve_start", d=int(d), constant=str(c))
    try:
        rank = E.rank(proof=False)
        gens = E.gens(proof=False)
        emit("curve_data", d=int(d), rank=int(rank), gens=[str(P) for P in gens], seconds=time.time()-t0)
    except Exception as exc:
        emit("rank_error", d=int(d), error=repr(exc), seconds=time.time()-t0)
        continue

    bound = 150 if len(gens) == 1 else (35 if len(gens) == 2 else (10 if len(gens) == 3 else 4))
    seen = set()
    try:
        if gens:
            ranges = [range(-bound, bound + 1) for _ in gens]
            for coeffs in itertools.product(*ranges):
                if all(n == 0 for n in coeffs):
                    continue
                P = E(0)
                for n, G in zip(coeffs, gens):
                    P += n*G
                if P.is_zero():
                    continue
                X, Y = QQ(P[0]), QQ(P[1])
                if X.denominator() != 1 or Y.denominator() != 1:
                    continue
                X, Y = ZZ(X), ZZ(Y)
                if X % d or Y % (d*d):
                    continue
                x, y, z = X//d, Y//(d*d), -d
                if verify(x, y, z):
                    key = (int(x), int(y), int(z))
                    if key not in seen:
                        seen.add(key)
                        emit("surface_hit", source="lattice", d=int(d), coefficients=list(coeffs), x=str(x), y=str(y), z=str(z), x_digits=len(str(abs(x))))
    except Exception as exc:
        emit("lattice_error", d=int(d), error=repr(exc))

    try:
        pts = E.integral_points(both_signs=True)
        emit("integral_points", d=int(d), count=len(pts))
        for P in pts:
            X, Y = ZZ(P[0]), ZZ(P[1])
            if X % d or Y % (d*d):
                continue
            x, y, z = X//d, Y//(d*d), -d
            assert verify(x, y, z)
            key = (int(x), int(y), int(z))
            if key not in seen:
                seen.add(key)
                emit("surface_hit", source="integral_points", d=int(d), point=str(P), x=str(x), y=str(y), z=str(z), x_digits=len(str(abs(x))))
    except Exception as exc:
        emit("integral_error", d=int(d), error=repr(exc))

    emit("curve_done", d=int(d), hits=len(seen), seconds=time.time()-t0)
