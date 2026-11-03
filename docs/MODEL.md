# Model, provenance and validation

FLOWSTATE uses one EPANET 2.2 engine for arbitrary user-designed networks.
There is no separate fixed-topology predictor or random result generator.

## Equations and units

- Nodes enforce water conservation. Pressure-dependent demand separately reports requested, delivered and unmet demand.
- Pipe loss uses Darcy–Weisbach with length/diameter/roughness in metres. Independent Colebrook checks verify the conversion and loss calculation.
- Fixed-impeller pump scaling uses `H(Q,n) = r² H0(Q/r)`. Shaft power uses the supplied reference curve and `r³`, with density correction. Motor and drive efficiencies are separate.
- Shutoff power is nonzero when an operating pump has zero flow. A stopped pump has zero pump power in this model.
- Control events preserve the requested speed when restarting. Changing a stopped pump's speed does not start it.
- Tanks use the solver's storage balance. Energy and delivered volume are integrated using the left endpoint of each report interval; the final sample is not counted as an extra interval.
- Coordinates are metres. Geometric Z and hydraulic elevation are distinct, and the user must confirm their mapping. The drawing does not silently redefine a reservoir's total head.

## CAD

The importer reads DXF centreline entities, including transformed blocks and curved paths. It explicitly converts units, merges endpoints in 3D, and can split a T-junction where an endpoint lies on a segment. Geometric crossings inside two segments are not automatically connected. Different-height crossings remain disconnected. File hashes, selected layers, tolerances and skipped entities are retained.

DXF does not establish pump type, pipe diameter, roughness, demand or boundary pressure. These remain user-confirmed model inputs. Native DWG/STEP/IGES/solid-part reconstruction is outside this release.

## Calibration and optimization

Sensor mappings explicitly identify a pressure node and flow link. Power measurements mean total electrical input of all pumps. Timestamps require timezones and exact report-time alignment.

Calibration fits one pump-curve multiplier while holding other inputs fixed. It scales reference head and shaft power together, reserves the last 30% of records, and retains the declared source. Same-solver synthetic recovery only checks the implementation.

Optimization searches pump combinations and a finite speed grid. It rejects unmet demand, insufficient pressure, out-of-curve operation, excess motor power and lower terminal storage. It does not guarantee a global optimum and does not yet enforce a validated NPSH margin.

## Verification

The retained tests cover arbitrary topology, pump-curve consistency, independent friction calculation, stop/shutoff/low-pressure behavior, event handling, tank balance, interval integration, CAD topology, malformed observations, synthetic parameter recovery, persistent queue semantics, native Qt editing and file persistence. A network-blocked test verifies that computation opens no network connection.

Frozen distributions run the actual packaged EPANET library, parse the sample CAD and render Qt widgets with the offscreen platform before an archive is accepted. This checks packaging completeness; it does not validate performance on every physical device or replace empirical validation.

## Sources

- [EPA EPANET](https://www.epa.gov/water-research/epanet), *EPANET 2.2 User Manual*, EPA/600/R-20/133.
- [WNTR hydraulic simulation](https://usepa.github.io/WNTR/hydraulics.html): engine and headloss-model distinctions.
- Kasat et al. (2023), [Developing a digital twin of centrifugal pump for performance evaluation](https://doi.org/10.1016/j.matpr.2022.09.574).
- 马斯卓等 (2024), [基于数字孪生的多级离心泵模型降阶与检验](https://doi.org/10.3969/j.issn.1674-8530.23.0086).
- Singh et al. (2024), [Digital Twin Framework for Leakages Detection](https://doi.org/10.1016/j.ifacol.2024.05.048).
- Kumar et al. (2024), [Digital twin-assisted AI framework for bearing diagnosis](https://doi.org/10.1016/j.measurement.2024.115013).
- [Hydraulic Institute pump curves](https://datatool.pumps.org/pump-fundamentals/pump-curves.html).
- [ezdxf](https://ezdxf.readthedocs.io/en/stable/disassemble.html): geometry and block decomposition.

Research-paper discussion was based on verified bibliographic records and public abstracts where full text was not available. This release does not claim to reproduce those papers' neural-network or CFD algorithms.
