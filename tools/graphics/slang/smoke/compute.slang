// Smoke test: a small compute shader every target can compile.

RWStructuredBuffer<float> output;

[shader("compute")]
[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    output[id.x] = sin(float(id.x)) * 2.0;
}
