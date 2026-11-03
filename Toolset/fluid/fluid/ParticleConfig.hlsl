// Particle pool sizes must match Probe.ini resource arrays, dispatch and draw ranges.
// Every slot is one tracked character with its own block of primary particles.
// SLOT_COUNT is what the pools hold; AssignSlots fills only the first
// $slot_count of them (IniParams[253].z).
static const uint SLOT_COUNT=8;
static const uint PRIMARY_PER_SLOT=1024;
static const uint PRIMARY_COUNT=SLOT_COUNT*PRIMARY_PER_SLOT;
// Characters the capture hooks of one frame can report. A candidate is the
// anchor rows ResolveSurface writes, after one header row: 1+16*9 rows.
static const uint CANDIDATE_CAPACITY=16;
static const uint CANDIDATE_ROWS=9;
// SurfaceProfiles rows per emitter id: priority and barycentric, forward,
// up, then the mod's look (strength offset, custom color, absorption depth,
// stream width) and its tint, which LoadProfilesJSON.hlsl writes and AssignSlots copies.
static const uint PROFILE_ROWS=5;
static const uint AIRBORNE_SPLASH_COUNT=2048;
static const uint PUDDLE_COUNT=4096;
static const uint SPLASH_COUNT=AIRBORNE_SPLASH_COUNT+PUDDLE_COUNT;
static const uint EVENT_CAPACITY=2048;
static const float FLUID_RADIUS_DEFAULT=0.0028000000;
// nhd-pick: runtime particle radius (IniParams[44].x). Zero keeps the compiled
// default, so mods that never set it behave exactly as before.
// nhd-pick: launch speed in metres/second (IniParams[46].x). Zero keeps the
// speed the strength level implies, so old mods are untouched.
float fluidLaunchSpeedOf(float4 row){float v=row.x;return v>0?min(v,6.0):0;}
float fluidRadiusOf(float4 row){float r=row.x;return r>1e-6?r:FLUID_RADIUS_DEFAULT;}
// Seconds a stream particle lives; each bank re-emits its slots once per lifetime.
static const float PRIMARY_LIFETIME=1.1; // nhd-pick: reaches the floor from chest height (was .48)

static const uint BASE_PRIMARY_COUNT=1024; // nhd-pick (was 512): same particles per second // Reserved second bank never consumes baseline slots.
