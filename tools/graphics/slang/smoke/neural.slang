// Smoke test: an experimental import from slang-standard-module-<version>/.

import slang.neural;

RWStructuredBuffer<float> output;

[shader("compute")]
[numthreads(1, 1, 1)]
void main()
{
    output[0] = 1.0;
}
