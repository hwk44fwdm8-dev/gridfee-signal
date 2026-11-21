# TSO of each of the 22 zones: what was checked

The zone list (ids, names, NUTS2 regions) comes from the owner's drafts. The drafts
also named a TSO per zone; that column was checked against the public sources below
(read on 2026-11-21) and four zones were changed.

The sources describe control areas by federal state, not by NUTS2 region. A zone that
lies in a state shared by two TSOs is marked **split**: its TSO is the one the sources
name for most of the state, and the assignment should be confirmed against a control
area map before results are relied on.

## Sources

- **[50Hertz]** 50hertz.com/de/Unternehmen: "Das 50Hertz-Netzgebiet umfasst die
  Bundesländer Brandenburg, Mecklenburg-Vorpommern, Sachsen, Sachsen-Anhalt und
  Thüringen sowie die Stadtstaaten Berlin und Hamburg."
- **[TenneT]** de.wikipedia.org/wiki/Tennet_TSO: "Die Übertragungsleitungen der Tennet
  TSO sind vorwiegend in den Bundesländern Bayern, Bremen, Hessen, Niedersachsen und
  Schleswig-Holstein sowie in Teilen von Nordrhein-Westfalen zu finden."
- **[Amprion]** amprion.net/Amprion/Unternehmen: the grid "reicht von der Nordsee bis
  zu den Alpen und erstreckt sich über sieben Bundesländer"; de.wikipedia.org/wiki/Amprion:
  the control area lies "schwerpunktmäßig im Westen und Südwesten Deutschlands. Es
  reicht von Niedersachsen im Norden bis ins Ostallgäu im Süden Bayerns", with the
  densest grid in Nordrhein-Westfalen and a substation at Kelsterbach (Hessen).
- **[TransnetBW]** transnetbw.de: operates the transmission grid in Baden-Württemberg.

No source read here lists Amprion's seven states by name. Rheinland-Pfalz and Saarland
are assigned to Amprion because they are western states that none of the other three
TSOs' area descriptions include.

## Result

| zone | NUTS2 | draft | used | check |
| --- | --- | --- | --- | --- |
| DE01 Berlin | DE30 | 50Hertz | 50Hertz | confirmed [50Hertz] |
| DE02 Brandenburg | DE40 | 50Hertz | 50Hertz | confirmed [50Hertz] |
| DE03 Mecklenburg-Vorpommern | DE80 | 50Hertz | 50Hertz | confirmed [50Hertz] |
| DE04 Sachsen | DED2, DED4, DED5 | 50Hertz | 50Hertz | confirmed [50Hertz] |
| DE05 Sachsen-Anhalt | DEE0 | 50Hertz | 50Hertz | confirmed [50Hertz] |
| DE06 Thüringen | DEG0 | 50Hertz | 50Hertz | confirmed [50Hertz] |
| DE07 Hamburg/Schleswig-Holstein | DE60, DEF0 | TenneT | TenneT | **split**: Schleswig-Holstein is TenneT [TenneT], Hamburg is 50Hertz [50Hertz] |
| DE08 Niedersachsen-Nord | DE93, DE94 | TenneT | TenneT | **split**: TenneT [TenneT]; Amprion reaches into western Niedersachsen [Amprion] |
| DE09 Niedersachsen-Süd | DE91, DE92 | TenneT | TenneT | confirmed [TenneT] |
| DE10 NRW-Nord/Ost | DEA3, DEA4, DEA5 | TenneT | **Amprion** | **changed, split**: TenneT is only in parts of NRW [TenneT]; Amprion's densest grid is in NRW [Amprion] |
| DE11 NRW-West | DEA1, DEA2 | TenneT | **Amprion** | **changed**: as DE10 |
| DE12 Bayern-Nord | DE23–DE26 | TenneT | TenneT | confirmed [TenneT] |
| DE13 Bayern-Mitte | DE21, DE22 | TenneT | TenneT | confirmed [TenneT] |
| DE14 Bayern-Süd/West | DE27 | TenneT | TenneT | **split**: TenneT for Bayern [TenneT], but Amprion reaches the Ostallgäu, which lies in this zone [Amprion] |
| DE15 Bremen | DE50 | Amprion | **TenneT** | **changed** [TenneT] |
| DE16 Hessen | DE71, DE72, DE73 | Amprion | **TenneT** | **changed, split**: TenneT for most of Hessen [TenneT]; Amprion is present in the south [Amprion] |
| DE17 Rheinland-Pfalz | DEB1, DEB2, DEB3 | Amprion | Amprion | by exclusion, see above |
| DE18 Saarland | DEC0 | Amprion | Amprion | by exclusion, see above |
| DE19–DE22 Baden-Württemberg | DE11–DE14 | TransnetBW | TransnetBW | confirmed [TransnetBW] |
