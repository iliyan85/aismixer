# W1 illustrative world profiles

These are separate version-1 W1 files. Historical B3.1 files remain unchanged.
The waypoint catalogue copies the sixteen named coordinates from the qualified
[B3.1 profile](../../geo/profiles/black_sea_demo.yaml), in the same order, and
repeats Durankulak exactly once for explicit closure. The loader removes only
that closing entry. Fixed W1 numeric IDs 1 through 16 identify catalogue points;
adding or reordering other entities does not change them.

The copied source values are coarse representative settlement positions rounded
to 0.01 degree. Their original coordinates, pinned public source revisions,
licensing and varying precision are preserved in the existing
[provenance record](../../geo/profiles/PROVENANCE.md). No source lookup, new
geographic verification or dependency download is performed for W1. Encoding
the values as nanodegrees does not improve their geographic accuracy.

The catalogue is neither a coastline nor a water/land mask. Its points are not
surveyed harbours or qualified marine waypoints. Illustrative geodesic routes
may cross land; no clipping or plausible-port routing is applied.

The Varna and Balchik circular regions use the same coarse representative
centres, with illustrative 20 km and 10 km radii. Every sampled receiver is an
**unqualified synthetic candidate**, which may be offshore or on unsuitable
terrain. Configured AMSL elevations remain AMSL; no geoid or terrain conversion
to ellipsoidal or above-ground height is supplied. Ship antenna offsets are
fixed above a modeled waterline whose ellipsoidal height remains unresolved.

The scenario exercises world navigation, coupled equipment power and abstract
effective-reach schedules. It simulates no AIS TDMA behavior or RF reception,
and emits no NMEA or fabricated successful reception.
