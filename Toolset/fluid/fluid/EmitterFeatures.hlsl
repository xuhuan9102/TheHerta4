#ifndef LIQUID_EMITTER_FEATURES
#define LIQUID_EMITTER_FEATURES
// Private to this mod; an unbound table retains legacy exports' global controls.
Buffer<uint> emitterFeatures:register(t9);
bool emitterTailEnabled(uint id){
 uint count;emitterFeatures.GetDimensions(count);
 if(id<count)return (emitterFeatures[id]&1u)!=0u;
 uint flags=IniParams[60].x>1e-6?(uint)IniParams[60].x:3u;
 return (flags&2u)!=0u;
}
bool emitterSplashEnabled(uint id){
 uint count;emitterFeatures.GetDimensions(count);
 if(id<count)return (emitterFeatures[id]&2u)!=0u;
 return IniParams[61].x>.5?IniParams[61].y>.5:true;
}
#endif
