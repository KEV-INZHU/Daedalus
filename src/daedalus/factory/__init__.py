"""The factory: a queue of work orders run through the gate, one after another.

A single `daedalus run` turns one goal into one accepted candidate. The factory
puts a line around that station:

    orders (committed specs) -> pick the next ready one -> branch -> build/verify/review
    -> ship exactly the accepted candidate as one commit, or park the attempt

Nothing here weakens the gate. The factory only decides *which* contract runs
next and what happens to the working tree afterwards; acceptance is still the
core's predicate, and only an ACCEPTED run is ever shipped.
"""
