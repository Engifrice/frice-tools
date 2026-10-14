// Doubles every element of a buffer; compiled to DXIL and to SPIR-V.
RWStructuredBuffer<float> values : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    values[id.x] = values[id.x] * 2.0;
}
