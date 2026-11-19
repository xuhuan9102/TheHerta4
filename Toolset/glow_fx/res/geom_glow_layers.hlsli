struct GlowLayers
{
    float4 front : SV_Target0;
    float4 back : SV_Target1;
    float2 coverage : SV_Target2;
};

void addBack(inout float4 layer, float4 emission)
{
    if (any(layer.rgb > 0.0))
        layer.a = min(layer.a, emission.a);
    else
        layer.a = emission.a;
    layer.rgb += emission.rgb;
}

void addEmission(inout GlowLayers layers, inout float nearestDepth, float4 emission, float coverage, float weight)
{
    emission.rgb *= weight;
    if (!any(emission.rgb > 0.0))
        return;

    float reference = max(nearestDepth, emission.a);
    float tolerance = max(reference, 0.000001) * 0.004;
    if (nearestDepth - emission.a > tolerance)
    {
        addBack(layers.back, emission);
        layers.coverage.y += coverage * weight;
    }
    else
    {
        // Keep the near layer's entire depth interval distinct from the back layer.
        if (any(layers.front.rgb > 0.0) && reference - layers.front.a > tolerance)
        {
            addBack(layers.back, layers.front);
            layers.front = 0;
            layers.coverage.y += layers.coverage.x;
            layers.coverage.x = 0.0;
        }
        addBack(layers.front, emission);
        layers.coverage.x += coverage * weight;
        nearestDepth = reference;
    }
}

GlowLayers normalizeLayers(GlowLayers layers, float weight)
{
    layers.front.rgb /= weight;
    layers.back.rgb /= weight;
    layers.coverage /= weight;
    return layers;
}
