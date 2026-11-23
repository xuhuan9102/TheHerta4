// **** RESPONSIVE UI SHADER (Dynamic Sampling - 3dmigoto Compatible) ****
// Contributors: SinsOfSeven, Fixed by Assistant
// Panel filtering uses explicit texel reads.

Texture1D<float4> IniParams : register(t120);

#define SIZE IniParams[87].xy
#define OFFSET IniParams[87].zw

struct vs2ps {
	float4 pos : SV_Position0;
	float2 uv : TEXCOORD1;
};

#ifdef VERTEX_SHADER
void main(
		out vs2ps output,
		uint vertex : SV_VertexID)
{
	float2 BaseCoord,Offset;
	Offset.x = OFFSET.x*2-1;
	Offset.y = (1-OFFSET.y)*2-1;
	BaseCoord.xy = float2((2*SIZE.x),(2*(-SIZE.y)));
	switch(vertex) {
		case 0:
			output.pos.xy = float2(BaseCoord.x+Offset.x, BaseCoord.y+Offset.y);
			output.uv = float2(1,0);
			break;
		case 1:
			output.pos.xy = float2(BaseCoord.x+Offset.x, 0+Offset.y);
			output.uv = float2(1,1);
			break;
		case 2:
			output.pos.xy = float2(0+Offset.x, BaseCoord.y+Offset.y);
			output.uv = float2(0,0);
			break;
		case 3:
			output.pos.xy = float2(0+Offset.x, 0+Offset.y);
			output.uv = float2(0,1);
			break;
		default:
			output.pos.xy = 0;
			output.uv = float2(0,0);
			break;
	};
	output.pos.zw = float2(0, 1);
}
#endif

#ifdef PIXEL_SHADER
Texture2D<float4> tex : register(t100);

// The game backbuffer used by the 3Dmigoto overlay is not an sRGB render
// target. Texture sampling converts the PNG's sRGB RGB values to linear, so
// encode them back before writing the overlay or the UI appears too dark.
float linear_to_srgb_channel(float value)
{
    value = max(value, 0.0f);
    if (value <= 0.0031308f)
        return value * 12.92f;
    return 1.055f * pow(value, 1.0f / 2.4f) - 0.055f;
}

float3 linear_to_srgb(float3 value)
{
    return float3(
        linear_to_srgb_channel(value.r),
        linear_to_srgb_channel(value.g),
        linear_to_srgb_channel(value.b)
    );
}

float4 sample_panel(float2 uv, int2 size, bool use_point_filter)
{
    float2 pixel = uv * size;
    int2 last = size - 1;
    float4 color = 0;
    if (use_point_filter) {
        color = tex.Load(int3(clamp(int2(floor(pixel)), 0, last), 0));
    } else {
        // Center the bilinear footprint on texels and clamp at the edges.
        pixel -= 0.5f;
        int2 lower = int2(floor(pixel));
        int2 upper = lower + 1;
        float2 weight = frac(pixel);
        lower = clamp(lower, 0, last);
        upper = clamp(upper, 0, last);

        float4 top = lerp(tex.Load(int3(lower, 0)), tex.Load(int3(upper.x, lower.y, 0)), weight.x);
        float4 bottom = lerp(tex.Load(int3(lower.x, upper.y, 0)), tex.Load(int3(upper, 0)), weight.x);
        color = lerp(top, bottom, weight.y);
    }
    return color;
}

void main(vs2ps input, out float4 result : SV_Target0)
{
    float2 dims;
    tex.GetDimensions(dims.x, dims.y);
    if (!dims.x || !dims.y) discard;

    input.uv.y = 1 - input.uv.y;

    // 计算当前纹理在屏幕上的实际缩放比例
    // 顶点四边形尺寸为 (2*SIZE.x, 2*SIZE.y)
    float2 scale = float2(2 * SIZE.x, 2 * SIZE.y) / dims;
    float maxScale = max(scale.x, scale.y);

    // 动态采样：放大或 1:1 时用点采样（锐利，与原 Load 语义一致），缩小时用线性采样（抗锯齿）
    result = sample_panel(input.uv, int2(dims), maxScale >= 1.0f);
    result.rgb = linear_to_srgb(result.rgb);
}
#endif
