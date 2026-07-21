# llama.cpp Profiles

`llama-fast.ini` describes the current Qwen 3B worker tuned for the GTX 1660 Ti.
`llama-deep.ini` is a disabled on-demand profile for a future 7B planner/reviewer.

The INI files are documentation and launch inputs for a future profile manager; they do
not start servers by themselves. Keep only one large model resident at a time on this
hardware. LiteLLM aliases (`local-fast`, `local-plan`, `local-review`) remain stable even
when their physical backend changes.
