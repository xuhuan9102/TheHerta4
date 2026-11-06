// Drawn only at a selected character mesh's normal-material pass.
#define FL_SCAN_SPEED 0.0405
#define FL_PHASE 0.0
#define FL_DIRECTION 1.0
#define FL_INTENSITY 2.6
#define FL_CORE_WIDTH 0.024
#define FL_HALO_WIDTH 0.095
#define FL_CORE_GAIN 2.2
#define FL_HALO_GAIN 0.18
#define FL_BASE_GAIN 0.08
#define FL_HUE_SPEED 0.45
#define FL_HUE_BANDS 6.0
#define FL_CORE_WHITE_MIX 0.48
#define FL_EMISSION_BASE 1.3
#define FL_EMISSION_GAIN 0.8
#define FL_EMISSION_CAP 4.0
#define FL_SURFACE_GAIN 0.35
#define FL_RIM_POWER 4.0
#define FL_EFFECT_MODE 2
#define FL_SCAN_AXIS 2
#define FL_SCAN_MIN 0.0
#define FL_SCAN_MAX 1.8
#define FL_MATERIAL_MAGIC 28463

Texture1D<float4> IniParams : register(t120);
cbuffer Scene : register(b0) { float4 scene[204]; }
cbuffer Object : register(b1) { float4 objectRows[29]; }
struct MaterialInput {
    // Preserve the native VS/PS register linkage: TEXCOORD2 must stay at v2.
    float4 padding0 : TEXCOORD0;
    float4 padding1 : TEXCOORD1;
    float4 normalWorldX : TEXCOORD2;
    float4 tangentWorldY : TEXCOORD3;
    float4 bitangentWorldZ : TEXCOORD4;
};

struct FlowOutput {
    float4 color : SV_Target0;
    float4 auxiliary1 : SV_Target1;
    float4 auxiliary2 : SV_Target2;
    float4 auxiliary3 : SV_Target3;
};
FlowOutput main(MaterialInput input)
{
    float3 glow = 0;
    float4 control = IniParams.Load(int2(244, 0));
    [branch] if (control.x == FL_MATERIAL_MAGIC) {
        float3 world = float3(input.normalWorldX.w, input.tangentWorldY.w, input.bitangentWorldZ.w);
        float3 local = world.x * objectRows[4].xyz + world.y * objectRows[5].xyz
                     + world.z * objectRows[6].xyz + objectRows[7].xyz;
        float y = saturate((local[FL_SCAN_AXIS] - FL_SCAN_MIN) / max(FL_SCAN_MAX - FL_SCAN_MIN, 0.0001));
        // DOWN starts at the high end of the model's selected local axis.
        if (FL_DIRECTION > 0) y = 1.0 - y;
        float head = 1.0 - abs(2.0 * frac(control.y * FL_SCAN_SPEED + FL_PHASE) - 1.0);
        head = head * head * (3.0 - 2.0 * head);
        float distance = abs(y - head);
        float core = exp(-pow(distance / max(FL_CORE_WIDTH, 0.0001), 2.0));
        float halo = exp(-pow(distance / max(FL_HALO_WIDTH, 0.0001), 2.0));
        float3 view = scene[55].xyz - world;
        view *= rsqrt(max(dot(view, view), 1e-12));
        float3 normal = input.normalWorldX.xyz;
        normal *= rsqrt(max(dot(normal, normal), 1e-12));
        float rim = pow(saturate(1.0 - abs(dot(normal, view))), FL_RIM_POWER);
        float coverage = FL_EFFECT_MODE == 0 ? rim : FL_EFFECT_MODE == 1 ? FL_SURFACE_GAIN : max(rim, FL_SURFACE_GAIN);
        float h6 = frac(2.0 * y + control.y * FL_HUE_SPEED) * FL_HUE_BANDS;
        float3 color = saturate(float3(abs(h6 - 3.0) - 1.0, 2.0 - abs(h6 - 2.0), 2.0 - abs(h6 - 4.0)));
        color = lerp(color, 1.0, core * FL_CORE_WHITE_MIX);
        float emission = FL_EMISSION_BASE + min(FL_INTENSITY, FL_EMISSION_CAP) * FL_EMISSION_GAIN;
        glow = color * emission * coverage * FL_INTENSITY
             * (FL_BASE_GAIN + core * FL_CORE_GAIN + halo * FL_HALO_GAIN);
    }
    FlowOutput output;
    output.color = float4(glow, 0);
    output.auxiliary1 = output.auxiliary2 = output.auxiliary3 = 0;
    return output;
}
