#version 450
// Smoke test: GLSL input with GLSL-only built-in functions.
// Slang treats a file as GLSL only when #version is on its first line, and a
// file named glsl.slang clashes with Slang's built-in glsl module.

layout(local_size_x = 8) in;
layout(std430, binding = 0) buffer Out { uint data[]; };
layout(binding = 1, rgba8) uniform writeonly image2D img;

void main()
{
    uint v = packHalf2x16(vec2(1.0, 2.0));
    atomicAdd(data[0], v);
    imageStore(img, ivec2(gl_GlobalInvocationID.xy), vec4(unpackUnorm4x8(v)));
    data[gl_LocalInvocationIndex] = bitfieldReverse(v) + uint(roundEven(1.5));
}
