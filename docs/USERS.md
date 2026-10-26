# User guide
A description on how different users should be able to interact with the site

## Workflows

Auto Analyze is the X-ray operator workflow: simple outcomes, a read-only image
and short To Do instructions. Analyze provides detailed measurements, manual
corrections, reports and exports for technicians and medical physicists.
History & Trends supports comparisons over time. There is no separate view
selector; both workflows use the same measurements and acceptance criteria.

All uploads use one page. Check the institution read from DICOM, or enter it
when absent; supply the phantom ID and operator to identify the scan. Select
Auto Analyze or Analyze step by step before submitting. Results save during
analysis. Advanced settings contains validation, reference selection and exports;
Back to results returns to recorded results without recomputing them.

To Do guidance is available in Auto Analyze and the manual results step.
Troubleshooting content is maintained in `solutions/`; see
[`solutions/README.md`](../solutions/README.md) for authoring instructions.

## X-ray Operator
Primary goal: Perform routine X-ray quality control measurements and submit the results with minimal effort.

The X-ray operator works in a hospital or field location, often with limited resources and technical support. Their primary responsibility is acquiring diagnostic X-ray images for patients. Quality control is an additional responsibility rather than their main task.

The operator may have limited knowledge of X-ray equipment physics and quality assurance procedures.

User preferences
- Wants to complete quality control tasks quickly and easily.
- Prefers simple instructions without complicated technical terminology.
- Wants to know whether the equipment is functioning correctly.
- Wants clear guidance when something goes wrong.
- Prefers not to interpret technical measurements.
- Wants to minimize interruptions to clinical work.
- Values reassurance that quality control measurements have been performed correctly.

Challenges
- Limited time for quality control activities.
- Limited training in equipment quality assurance.
- Potential language barriers.
- Limited access to technical support.
- Working in environments with unreliable internet connectivity.

Key motivation: "I want to know that my X-ray equipment is working correctly without spending too much time on technical tasks."

## X-ray Technician
Primary goal: Understand equipment performance, identify problems, and find practical solutions.

The X-ray technician is responsible for maintaining and troubleshooting X-ray equipment. The technician has technical knowledge of X-ray systems but may not have specialized training in medical physics. They may be responsible for multiple medical devices and have limited access to specialized tools, spare parts, and external expertise.

User preferences
- Wants to understand why a quality control measurement failed.
- Prefers practical information that helps identify possible causes.
- Wants to distinguish between minor deviations and serious equipment problems.
- Values information about previous equipment performance.
- Wants to recognize recurring problems.
- Prefers clear, actionable troubleshooting recommendations.
- Wants to solve problems independently whenever possible.
- Values access to expert advice when problems exceed their expertise.
- Want to see degradation of equiment to predict problems in advance

Challenges
- Limited availability of specialized diagnostic equipment.
- Limited access to spare parts.
- Difficulty distinguishing equipment defects from measurement errors.
- Limited access to manufacturer support.
- Maintaining different X-ray systems with varying technical specifications.

Key motivation: "I want to understand what is wrong with the equipment and determine how to solve the problem."

## Medical Physicist
Primary goal: Ensure the long-term quality, reliability, and safety of X-ray equipment through remote technical oversight.

The medical physicist has specialized knowledge of X-ray equipment performance, radiation physics, and quality assurance. They may be responsible for supporting multiple X-ray systems across different MSF projects and countries. Unlike the operator and technician, the medical physicist is often not physically present at the hospital.

They depend on measurements and information collected by local staff to evaluate equipment performance.

User preferences
- Wants reliable, objective, and reproducible quality control measurements.
- Prefers access to detailed technical information.
- Values historical measurements to identify gradual deterioration.
- Wants to distinguish isolated measurement errors from systematic equipment problems.
- Wants to compare equipment performance over time.
- Prefers to prioritize equipment requiring attention.
- Wants sufficient information to make technical decisions remotely.
- Values consistent quality assurance procedures across locations.

Challenges
- Limited opportunities to inspect equipment physically.
- Dependence on the quality of measurements performed by local staff.
- Differences in equipment models and operating conditions.
- Limited information about maintenance history.
- Difficulty identifying developing problems before equipment failure.
- Supporting multiple locations with limited time and resources.

Key motivation: "I want to understand the condition of X-ray equipment remotely and identify problems before they affect clinical operations."