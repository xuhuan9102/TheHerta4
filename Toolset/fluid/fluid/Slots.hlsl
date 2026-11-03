// Per-character emitter slots that AssignSlots.hlsl fills each frame.
// Slot rows 0-8 are the anchor in ResolveSurface's layout: right, up, forward,
// emitter point, (priority, profile id, area, seen this frame), character
// root, captured triangle a, b, c. The anchor survives frames the character
// is not drawn in, with the seen flag cleared.
// Includers declare IniParams first.
#ifndef LIQUID_SLOTS
#define LIQUID_SLOTS
static const uint SLOT_ROWS=16;
// xyz the character root that slot particles are stored relative to; w is 1
// while the slot is assigned. A freed slot drops its particles that frame.
static const uint SLOT_ROOT=9;
// x the time the slot's character was last drawn.
static const uint SLOT_TRACK=10;
// The root one frame before SLOT_ROOT, w 1 when known; AssignSlots
// extrapolates the character's motion from it.
static const uint SLOT_PREVIOUS_ROOT=11;
// The look of the character's mod, copied from its profile: x the strength
// level offset, y 1 for a custom color, z its absorption depth in meters,
// w the stream width scale. All zero keeps the global settings.
static const uint SLOT_LOOK=12;
// rgb the custom tint.
static const uint SLOT_TINT=13;
// nhd-pick: x 1 marks a NUMPAD9-only trickle emitter.
static const uint SLOT_TRICKLE=14;
#ifdef SLOT_STATE_WRITER
RWStructuredBuffer<float4> slots:register(u0);
#else
StructuredBuffer<float4> slots:register(t103);
#endif
float4 slotAnchor(uint s,uint k){return slots[s*SLOT_ROWS+k];}
// nhd-pick: w of the SLOT_TRICKLE row marks an emitter that runs while its part is drawn, ignoring NUMPAD1.
bool slotVisActive(uint s){return slots[s*SLOT_ROWS+SLOT_TRICKLE].w>.5 && IniParams[239].y-slots[s*SLOT_ROWS+SLOT_TRACK].x<.2;}
bool slotVisOn(uint s){return slots[s*SLOT_ROWS+SLOT_TRICKLE].w>.5;}
bool slotTrickle(uint s){return slots[s*SLOT_ROWS+SLOT_TRICKLE].x>.05;}
float slotTrickleScale(uint s){return clamp(slots[s*SLOT_ROWS+SLOT_TRICKLE].x,.1,8);}
bool slotSeen(uint s){return slots[s*SLOT_ROWS+4].w>.5;}
// The emitter id this slot is carrying. AssignSlots copies the candidate row
// verbatim (ResolveSurface.hlsl:75 stores IniParams[243].x there, the host
// hook's x243 = emitter index), so row 4 .y is the emitter's own id. A stale or
// out-of-range row falls back to the slot number, which is the pre-fix
// indexing: never worse than before, identical whenever the mod has one emitter.
uint slotEmitterId(uint s){
 float id=slots[s*SLOT_ROWS+4].y;
 return (id>=0&&id<SLOT_COUNT)?(uint)id:s;
}
// Per-emitter on/off. 42 packs emitters 0-3 and 43 packs 4-7, one gate per
// component: $fluid_emit_N in the ini (declared in [Constants], cycled by
// [KeyFluidEmitN], writable per frame by the animation driver). The gate is
// indexed by the emitter's own id, not by the slot -- AssignSlots hands slots
// out in arrival order, so slot 2 can carry emitter 0 and gating by slot would
// switch the wrong stream. Every gate starts at 1, so a mod that binds no keys
// behaves exactly as it did before the gates existed.
bool slotEmitOn(uint s){
 uint id=slotEmitterId(s);
 float4 g=(id<4u)?IniParams[42]:IniParams[43];
 return g[id&3u]>0.5;
}
// Per-emitter launch speed in m/s: 45 packs emitters 0-3 and 48 packs 4-7
// ($fluid_emitter_speed_N). Zero, or a lane this export never wrote, keeps the
// global $fluid_launch_speed / strength level path in FluidSolver.
float emitterSpeed(uint id){
 float4 v=(id<4u)?IniParams[45]:IniParams[48];
 float s=v[id&3u];
 return s>0?s:0;
}
bool slotLive(uint s){return slots[s*SLOT_ROWS+SLOT_ROOT].w>.5;}
float3 slotRoot(uint s){return slots[s*SLOT_ROWS+SLOT_ROOT].xyz;}
uint primarySlot(uint i){return i/PRIMARY_PER_SLOT;}
// The nozzle setting the slot emits with: the global strength level (253.y)
// shifted by the character's offset, from .28 at level 0 up to 3.94 at 7.
float slotDrive(uint s){
 if(slotTrickle(s))return .28;
 // nhd-pick: continuous, not stepped. The panel now names a launch speed and the
 // exporter writes the equivalent level here, so rounding would quantise it back.
 float level=clamp(IniParams[253].y+slots[s*SLOT_ROWS+SLOT_LOOK].x,0,7);
 return level<.5?.28:.565+(level-1)*.5625;
}
// The character's stream width over the runtime's, scaling the nozzle
// cross-section and the rendered stream together; zero, as in a profile from
// before the setting, means 1.
float slotWidth(uint s){
 float width=slots[s*SLOT_ROWS+SLOT_LOOK].w;
 return width>0?clamp(width,.4,5):1;
}
// rgb the Beer-Lambert absorption per meter that reaches the tint at the
// absorption depth, w that depth in meters: the character's custom color or
// the global look (251).
float4 slotLiquid(uint s){
 float4 look=slots[s*SLOT_ROWS+SLOT_LOOK];
 float3 tint=IniParams[251].rgb;float depth=IniParams[251].w;
 if(look.y>.5){
  tint=slots[s*SLOT_ROWS+SLOT_TINT].rgb;depth=look.z;
  // nhd-pick: NUMPAD5/6 and 7/8 (Probe.ini) still shift a custom color: toward white, and thicker/thinner.
  float yellow=saturate(1-(IniParams[251].b-.03)/.95);
  if(!slotTrickle(s)){
  tint=lerp(float3(.98,.98,.98),tint,yellow);
  tint=lerp(tint,tint*float3(.9,.7,.3),saturate((.75-IniParams[251].g)/.25));
  depth=look.z*max(IniParams[251].w/.015,.001);
  }
 }
 depth=max(depth,.00002);
 return float4(-log(max(saturate(tint),.02))/depth,depth);
}
// Splash particles come from every slot, so their live flag carries the
// owner: velocityAlive.w is 1+slot while alive and 0 once dead.
float splashAliveTag(uint s){return 1+s;}
uint splashSlot(float aliveTag){return (uint)clamp(round(aliveTag)-1,0,SLOT_COUNT-1);}
#endif
