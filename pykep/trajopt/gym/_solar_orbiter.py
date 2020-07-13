import numpy as np
from math import pi
from pykep import DAY2SEC, epoch, ic2par
from pykep.core import fb_prop, fb_vel, lambert_problem
from pykep.planet import jpl_lp


class _solar_orbiter_udp():
    def __init__(
        self, t0=[epoch(0), epoch(1000)], multi_objective=False, tof_encoding="direct"
    ):
        """
        Args:
            - multi_objective (``bool``): when True the problem fitness will return also the time of flight as an added objective
            - tof_encoding (``str``): one of 'direct', 'eta' or 'alpha'. Selects the encoding for the time of flights
            - tof (``list`` or ``list`` of ``list``): time of flight bounds. As documented in ``pykep.mga_1dsm``

        """

        multi_objective = False
        eta_lb = 0.1
        eta_ub = 0.9
        rp_ub = 30

        # Redefining the planets as to change their safe radius. 350km was given as safe distance.
        earth = jpl_lp("earth")
        earth.safe_radius = (earth.radius + 350000) / earth.radius
        venus = jpl_lp("venus")
        venus.safe_radius = (venus.radius + 350000) / venus.radius
        seq = [
            earth,
            venus,
            earth,
            earth,
            venus,
        ]  # alternative: Launch-Venus-Venus-Earth-Venus
        tof = [[10, 600]] * (len(seq) - 1)

        # Sanity checks
        # 1 - Planets need to have the same mu_central_body
        if [r.mu_central_body for r in seq].count(seq[0].mu_central_body) != len(seq):
            raise ValueError(
                "All planets in the sequence need to have identical mu_central_body"
            )
        # 2 - tof encoding needs to be one of 'alpha', 'eta', 'direct'
        if tof_encoding not in ["alpha", "eta", "direct"]:
            raise TypeError("tof encoding must be one of 'alpha', 'eta', 'direct'")
        # 3 - tof is expected to have different content depending on the tof_encoding
        if tof_encoding == "direct":
            if np.shape(np.array(tof)) != (len(seq) - 1, 2):
                raise TypeError(
                    "tof_encoding is "
                    + tof_encoding
                    + " and tof must be a list of two dimensional lists and with length equal to the number of legs"
                )
        if tof_encoding == "alpha":
            if np.shape(np.array(tof)) != (2,):
                raise TypeError(
                    "tof_encoding is "
                    + tof_encoding
                    + " and tof must be a list of two floats"
                )
        if tof_encoding == "eta":
            if np.shape(np.array(tof)) != ():
                raise TypeError(
                    "tof_encoding is " + tof_encoding + " and tof must be a float"
                )
        # 4 - Check launch window t0. If defined in terms of floats transform into epochs
        if len(t0) != 2:
            raise TypeError(
                "t0 is " + t0 + " while should be a list of two floats or epochs"
            )
        if type(t0[0]) is not epoch:
            t0[0] = epoch(t0[0])
        if type(t0[1]) is not epoch:
            t0[1] = epoch(t0[1])

        self._seq = seq
        self._t0 = t0
        self._tof = tof
        self._tof_encoding = tof_encoding
        self._multi_objective = multi_objective
        self._eta_lb = eta_lb
        self._eta_ub = eta_ub
        self._rp_ub = rp_ub

        self._n_legs = len(seq) - 1
        self._common_mu = seq[0].mu_central_body

    def _decode_tofs(self, x):
        if self._tof_encoding == 'alpha':
            # decision vector is  [t0, T, a1, a2, ....]
            T = np.log(x[2:-2])
            return T / sum(T) * x[1]
        elif self._tof_encoding == 'direct':
            # decision vector is  [t0, T1, T2, T3, ... ]
            return x[1:-2]
        elif self._tof_encoding == 'eta':
            # decision vector is  [t0, n1, n2, n3, ... ]
            dt = self.tof
            T = [0] * self._n_legs
            T[0] = dt * x[1]
            for i in range(1, len(T)):
                T[i] = (dt - sum(T[:i])) * x[i + 1]
            return T

    def _compute_dvs(self, x):
        # 1 -  we 'decode' the times of flights and compute epochs (mjd2000)
        T = self._decode_tofs(x)  # [T1, T2 ...]
        ep = np.insert(T, 0, x[0])  # [t0, T1, T2 ...]
        ep = np.cumsum(ep)  # [t0, t1, t2, ...]
        # 2 - we compute the ephemerides
        r = [0] * len(self._seq)
        v = [0] * len(self._seq)
        for i in range(len(self._seq)):
            r[i], v[i] = self._seq[i].eph(ep[i])
        # 3 - we solve the lambert problems
        l = list()
        for i in range(self._n_legs):
            l.append(lambert_problem(
                r[i], r[i + 1], T[i] * DAY2SEC, self._common_mu, False, 0))
        # 4 - we compute the various dVs needed at fly-bys to match incoming
        # and outcoming
        DVfb = list()
        for i in range(len(l) - 1):
            vin = [a - b for a, b in zip(l[i].get_v2()[0], v[i + 1])]
            vout = [a - b for a, b in zip(l[i + 1].get_v1()[0], v[i + 1])]
            DVfb.append(fb_vel(vin, vout, self._seq[i + 1]))
        return (DVfb, l, ep)

    # Objective function
    def fitness(self, x):
        DVfb, lamberts, ep = self._compute_dvs(x)
        if self._tof_encoding == 'direct':
            T = sum(x[1:-2])
        elif self._tof_encoding == 'alpha':
            T = x[1]
        elif self._tof_encoding == 'eta':
            T = sum(self.eta2direct(x)[1:-2])

        # compute final flyby
        eph = self._seq[-1].eph(ep[-1])
        v_out = fb_prop(lamberts[-1].get_v2()[0], eph[1], x[-1], x[-2], self._seq[-1].mu_self)

        if self._multi_objective:
            return [np.sum(DVfb), T]  # TODO: adapt to inclination
        else:
            return [np.sum(DVfb)]

    def get_nobj(self):
        return self._multi_objective + 1

    def get_bounds(self):
        t0 = self._t0
        tof = self._tof
        n_legs = self._n_legs

        if self._tof_encoding == 'alpha':
            # decision vector is  [t0, T, a1, a2, ....]
            lb = [t0[0].mjd2000, tof[0]] + [1e-3] * (n_legs)
            ub = [t0[1].mjd2000, tof[1]] + [1.0 - 1e-3] * (n_legs)
        elif self._tof_encoding == 'direct':
            # decision vector is  [t0, T1, T2, T3, ... ]
            lb = [t0[0].mjd2000] + [it[0] for it in self._tof]
            ub = [t0[1].mjd2000] + [it[1] for it in self._tof]
        elif self._tof_encoding == 'eta':
            # decision vector is  [t0, n1, n2, ....]
            lb = [t0[0].mjd2000] + [1e-3] * (n_legs)
            ub = [t0[1].mjd2000] + [1.0 - 1e-3] * (n_legs)

        # add final flyby
        safe_distance = 350000
        pl = self._seq[-1]
        lb = lb + [-2 * pi, (pl.radius + safe_distance) / pl.radius]
        ub = ub + [2 * pi, 30]
        return (lb, ub)

    def get_nic(self):
        return 0

    def pretty(self, x):
        """pretty(x)

        Args:
            - x (``list``, ``tuple``, ``numpy.ndarray``): Decision chromosome, e.g. (``pygmo.population.champion_x``).

        Prints human readable information on the trajectory represented by the decision vector x
        """
        T = self._decode_tofs(x)
        ep = np.insert(T, 0, x[0])  # [t0, T1, T2 ...]
        ep = np.cumsum(ep)  # [t0, t1, t2, ...]
        DVfb, l = self._compute_dvs(x)
        print("Multiple Gravity Assist (MGA) problem: ")
        print("Planet sequence: ", [pl.name for pl in self._seq])

        print("Departure: ", self._seq[0].name)
        print("\tEpoch: ", ep[0], " [mjd2000]")
        print("\tSpacecraft velocity: ", l[0].get_v1()[0], "[m/s]")

        for pl, e, dv in zip(self._seq[1:-1], ep[1:-1], DVfb):
            print("Fly-by: ", pl.name)
            print("\tEpoch: ", e, " [mjd2000]")
            print("\tDV: ", dv, "[m/s]")

        print("Arrival: ", self._seq[-1].name)
        print("\tEpoch: ", ep[-1], " [mjd2000]")
        print("\tSpacecraft velocity: ", l[-1].get_v2()[0], "[m/s]")

        print("Time of flights: ", T, "[days]")
