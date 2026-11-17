import math


class Welford:
    __slots__ = ("n", "mean", "m2")

    def __init__(self):
        self.n, self.mean, self.m2 = 0, 0.0, 0.0

    def add(self, x):
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    def std(self):
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


class RuntimePredictor:
    K = 2
    Z = 1.65
    CLAMP = (0.02, 50.0)
    MIN_N_SPREAD = 30
    CV_W_DEFAULT = 0.10
    CV_C_DEFAULT = 0.5
    CV_C_NEW_CLASS = 1.0

    def __init__(self):
        self.tmpl = {}
        self.cls = {}
        self.typ = {}
        self.resid = {}
        self.version = {}
        self._cache = {}

    @staticmethod
    def template_key(view):
        return (view.client_id, view.query_type, view.template_id)

    def prior(self, client, qtype):
        c = self.cls.get((client, qtype))
        if c is not None and c.n >= 1:
            return c.mean
        t = self.typ.get(qtype)
        if t is not None and t.n >= 1:
            return t.mean
        return 1.0

    def _estimate(self, view):
        key = self.template_key(view)
        v = self.version.get(view.query_type, 0)
        hit = self._cache.get(key)
        if hit is not None and hit[0] == v:
            return hit[1], hit[2]
        n, total = self.tmpl.get(key, (0, 0.0))
        mh = (total + self.K * self.prior(view.client_id, view.query_type)) / (n + self.K)
        s = self.spread(view)
        self._cache[key] = (v, mh, s)
        return mh, s

    def m_hat(self, view):
        return self._estimate(view)[0]

    def spread(self, view):
        n = self.tmpl.get(self.template_key(view), (0, 0.0))[0]
        r = self.resid.get(view.query_type)
        cv_w = r.std() if r is not None and r.n >= self.MIN_N_SPREAD else self.CV_W_DEFAULT
        c = self.cls.get((view.client_id, view.query_type))
        if c is None or c.n == 0:
            cv_c = self.CV_C_NEW_CLASS
        elif c.n < self.MIN_N_SPREAD or c.mean <= 0:
            cv_c = self.CV_C_DEFAULT
        else:
            cv_c = c.std() / c.mean
        return math.sqrt(cv_w ** 2 + cv_c ** 2 / (n + 1) ** 2)

    def class_mean(self, client, qtype):
        return self.prior(client, qtype)

    def class_upper(self, client, qtype):
        r = self.resid.get(qtype)
        cv_w = r.std() if r is not None and r.n >= self.MIN_N_SPREAD else self.CV_W_DEFAULT
        c = self.cls.get((client, qtype))
        if c is None or c.n == 0:
            cv_c = self.CV_C_NEW_CLASS
        elif c.n < self.MIN_N_SPREAD or c.mean <= 0:
            cv_c = self.CV_C_DEFAULT
        else:
            cv_c = c.std() / c.mean
        return self.prior(client, qtype) * (1.0 + self.Z * math.sqrt(cv_w ** 2 + cv_c ** 2))

    def m_up(self, view):
        mh, s = self._estimate(view)
        return mh * (1.0 + self.Z * s)

    def m_lo(self, view):
        mh, s = self._estimate(view)
        return mh / (1.0 + self.Z * s)

    def observations(self, view):
        return self.tmpl.get(self.template_key(view), (0, 0.0))[0]

    def observe(self, view, runtime_s, bench_at_assign):
        if not bench_at_assign or bench_at_assign <= 0:
            return None
        m = min(max(runtime_s / bench_at_assign, self.CLAMP[0]), self.CLAMP[1])
        key = self.template_key(view)
        before = self.m_hat(view)
        self.resid.setdefault(view.query_type, Welford()).add(m / before)
        self.version[view.query_type] = self.version.get(view.query_type, 0) + 1
        entry = self.tmpl.setdefault(key, [0, 0.0])
        entry[0] += 1
        entry[1] += m
        self.cls.setdefault((view.client_id, view.query_type), Welford()).add(m)
        self.typ.setdefault(view.query_type, Welford()).add(m)
        return key, before, self.m_hat(view)