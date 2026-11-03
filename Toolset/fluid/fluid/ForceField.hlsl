// Force fields attached to an emitter slot.
//
// Each emitter owns FORCE_FIELDS_PER_SLOT fields, three float4 rows each. The
// rows are indexed by the emitter's own id, not by the character slot it happens
// to land in: the exporter writes them in emitter order, AssignSlots hands out
// slots in arrival order.
//   row 0  offsetType  xyz offset from the emitter anchor in its own basis, w type
//   row 1  axisRadius  xyz direction or axis in the same basis, w radius in meters
//   row 2  strength    x acceleration in m/s^2, y falloff exponent, w 1 while enabled
//
// The anchor rows are read where the field is applied, so a field written as an
// offset moves and turns with its emitter: no world-space bookkeeping needed.
//
// Falloff is (1 - r^2/R^2)^power over [0,R] and zero outside. The squared form
// is continuous in value and slope at the boundary and needs no sqrt, unlike the
// customary (1 - r/R)^power; both describe the same family of curves. power = 2
// is the shape the reference implementation uses, and the default the exporter
// writes, so an untouched field behaves exactly like the fixed curve did.
#ifndef LIQUID_FORCE_FIELD
#define LIQUID_FORCE_FIELD

// Probe.ini ResourceFluidForceField: SLOT_COUNT * FORCE_FIELDS_PER_SLOT * 3 rows.
Buffer<float4> forceField : register(t8);

static const uint FORCE_FIELDS_PER_SLOT=4;
// Emitters the table holds: the exporter's FORCE_FIELD_SLOT_COUNT, which is
// ParticleConfig.hlsl's SLOT_COUNT (8). A slot that remembers an id outside
// [0, this) falls back to its own slot index below.
static const uint FORCE_FIELD_SLOTS=8;
static const uint FF_DIRECTIONAL=0;
static const uint FF_RADIAL=1;
static const uint FF_VORTEX=2;

float forceFieldFalloff(float r2,float r2Max,float power){
  if(r2>=r2Max)return 0;
  float t=1-r2/r2Max;
  // power <= 1 keeps the plain (1 - t) shape; the clamp also stops a zero or
  // negative exponent from turning a hard edge into a spike.
  return pow(t,max(power,1));
}

// Per-field force switch ($fluid_force_enable_<E>_<K>, E = emitter id, K = the
// field's index inside that emitter). One variable per field, packed as a bit
// mask into one lane per emitter: 58 = emitters 0-3, 59 = 4-7, bit k = field k
// (the same k the exporter uses for that field's three rows), so eight emitters
// with four fields each still cost only two IniParams numbers.
//
// IniParams[41].w is the family flag. Probe.ini has `w41 = 0` inside the gate and
// the exporter rewrites that line to a literal 0/1 on every export (0 unless at
// least one field switch is off), so an export that never touched the switches
// leaves it 0 and every field follows its own exported enabled bit -- exactly
// the old behaviour. The 0 is written on purpose: IniParams has no namespace,
// so a 1 left behind by another fluid mod would otherwise switch this mod's
// fields off silently (the 58/59 masks would then be read: stale or zero).
bool emitterFieldOn(uint id,uint k){
  if(IniParams[41].w<.5)return true;
  float4 m=(id<4u)?IniParams[58]:IniParams[59];
  uint mask=(uint)max(m[id&3u],0);
  return ((mask>>k)&1u)!=0;
}

// Acceleration felt at `p` from the fields owned by the emitter `slot` carries,
// in the same character-root-local space the particles use. Returns 0 when the
// export has no field at all (IniParams[41].x zero), so an export without force
// fields costs one uniform read per particle and nothing else.
float3 forceFieldAccel(uint slot,float3 p){
  if(IniParams[41].x<.5)return 0;
  if(!slotLive(slot))return 0;
  float3 anchor=slotAnchor(slot,3).xyz-slotRoot(slot);
  // Rows 0-2 carry (right, -aim, in-plane) because FluidSolver's jet axis is -up.
  // The panel and the Blender preview both speak (right, 面内上, 喷射方向), so map
  // the rows back to that frame; otherwise a previewed offset would land mirrored.
  float3 right=slotAnchor(slot,0).xyz;
  float3 up=slotAnchor(slot,2).xyz;
  float3 forward=-slotAnchor(slot,1).xyz;
  float3 total=0;
  // Which emitter's fields this slot is carrying: row 4 .y is the emitter id
  // AssignSlots copied out of the candidate (ResolveSurface stores
  // IniParams[243].x there, the host hook's x243 = emitter index), and it is
  // written in the same iteration as the anchor rows, so it is valid whenever
  // slotLive(slot) held above. `slot` itself is only the slot number, and
  // AssignSlots hands those out to whichever candidate arrives first, so
  // slot*FORCE_FIELDS_PER_SLOT would read another emitter's field.
  // Out of range (or a negative id from a stale row) falls back to `slot`,
  // which is the pre-fix indexing: never worse than before, and identical to
  // the id whenever the export has a single emitter.
  float owner=slotAnchor(slot,4).y;
  uint fieldSlot=(owner>=0&&owner<FORCE_FIELD_SLOTS?(uint)owner:slot);
  uint base=fieldSlot*FORCE_FIELDS_PER_SLOT;
  // Fixed trip count with an early-out per field: no [unroll], so a longer
  // field table cannot turn into a compile-time explosion.
  [loop]for(uint k=0;k<FORCE_FIELDS_PER_SLOT;++k){
    // Per-field switch, after the global gate above and the row's own enabled
    // bit: all three have to pass.
    if(!emitterFieldOn(fieldSlot,k))continue;
    uint row=(base+k)*3;
    float4 strength=forceField[row+2];
    if(strength.w<.5)continue;
    float4 offsetType=forceField[row];
    float4 axisRadius=forceField[row+1];
    float3 center=anchor+right*offsetType.x+up*offsetType.y+forward*offsetType.z;
    float3 d=p-center;
    float r2=dot(d,d);
    float r2Max=axisRadius.w*axisRadius.w;
    if(r2>=r2Max)continue;
    float w=forceFieldFalloff(r2,r2Max,strength.y)*strength.x;
    if(abs(w)<1e-6)continue;
    uint type=(uint)offsetType.w;
    float3 axis=right*axisRadius.x+up*axisRadius.y+forward*axisRadius.z;
    float3 dir;
    if(type==FF_RADIAL){
      // Away from the centre; a negative strength pulls inward instead.
      dir=d*rsqrt(max(r2,1e-12));
    }else if(type==FF_VORTEX){
      // Swirl around the axis: tangent to the circle through p about the axis.
      float3 unitAxis=axis*rsqrt(max(dot(axis,axis),1e-12));
      float3 radial=d-unitAxis*dot(d,unitAxis);
      float3 tangential=cross(unitAxis,radial);
      dir=tangential*rsqrt(max(dot(tangential,tangential),1e-12));
    }else{
      // Directional: the axis row is the direction field, normalised here so a
      // hand-authored axis does not have to be unit length.
      dir=axis*rsqrt(max(dot(axis,axis),1e-12));
    }
    total+=dir*w;
  }
  return total*IniParams[41].y;
}
#endif
