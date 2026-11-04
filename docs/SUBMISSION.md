# Ustad

**Live demo:** https://prabindersinghh--ustad-web.us-east.modal.run
**Demo video:** (add link)
**Code:** (add link)

## In one line

An expert does a job once. Ustad turns it into a checkable procedure, then guides any worker through it hands-free and verifies every step on camera.

## The problem

What a factory job really takes is often in the head of one expert. When that person is away, the next worker is on their own, and a checklist can't see whether the work was done.

## What it does

- **Capture.** An expert records the job and talks through it. Ustad writes the steps and checks with SAM 3.1 that each step's target can be found.
- **Guide.** A worker hears one step at a time, in English or Hindi. When they finish, SAM 3.1 finds the part and Muse Spark must choose one of three tools: pass the step, flag an error, or ask for a better view.
- **Evidence.** Every check saves its photo, verdict and reason in a printable pack. Safety steps are never skipped by inference.

We tested Ustad in two factories: a textile clothing factory, and MCPL (Modern Colours Private Limited) in Ludhiana. We are starting with India, a huge market. Trial figures are still in progress.

## Measured on our own test runs

| | |
|---|---|
| From "done" to a decision | 4 to 5 s |
| Finished step to the next instruction | about 7 s |
| Short recording to a checked job card | 17 to 34 s |
| Cost of guiding a 4-step job | about $0.10 |

## Meta models

| Model | Role |
|---|---|
| Muse Spark 1.3 | Live decisions, through tool calls |
| SAM 3.1 | Finds the target part in each frame |
| Muse Voice Transcribe | Speech, including Hindi |
| Muse Glimmer 30B (open weights) | Second opinion after the job |

**Live guiding stays on the fast models.** On 8 test cases, 4 of them deliberately wrong, Muse Spark scored 8/8 at a 4.6 s median and Muse Glimmer 8/8 at 63 s, both with no false passes. That's too slow to guide a worker, but fine for an audit, so a supervisor can have Glimmer re-check a finished job, or only its doubtful steps. It runs today through NVIDIA's API, with the model id on the record. A failed call stays a failure and is never answered by another model. This is a small sample, and running Glimmer on our own GPU is not tested yet.

## Learning from real work

Every job produces labelled data: what was asked, what the camera saw, what was right. We built the pipeline to turn that into reinforcement learning data for Muse Glimmer. From one 42-second factory clip it produced 1,046 labelled examples (710 training, 336 held out). **Nothing has been trained yet, so there is no accuracy result.** The labels were made by an AI viewing the frames, not by a factory expert. The aim is a verifier that improves as the product is used.

## What we found, including the weak spots

On the factory clip, SAM 3.1 found the emergency stop and the blue parts but missed some smaller objects by name. Ustad then judged from the image alone, and it says so on the record. No error was flagged on that clip, so the caught error in the video comes from our own test run, and is labelled that way.

Voices in the video are synthetic.
