"""Query-shape holdout cannot be escaped by renaming relational bindings."""

import pytest

from text2sql.evaluation.dataset import sql_template_fingerprint


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "SELECT COUNT(*) AS Total FROM Fact a WHERE a.Value>=1",
            "SELECT COUNT(*) AS Label FROM dbo.Fact b WHERE b.Value>=2",
        ),
        ("SELECT Value AS x FROM Fact", "SELECT f.Value AS y FROM Fact f"),
        (
            "WITH x AS (SELECT a.Value AS Label FROM Fact a) SELECT x.Label FROM x",
            "WITH renamed AS (SELECT b.Value AS Other FROM Fact b) SELECT z.Other FROM renamed z",
        ),
        (
            "WITH x(Label) AS (SELECT a.Value FROM Fact a) SELECT x.Label FROM x",
            "WITH renamed(Other) AS (SELECT b.Value FROM Fact b) SELECT z.Other FROM renamed z",
        ),
        (
            "SELECT d.Label FROM (SELECT f.Value AS Label FROM Fact f) d",
            "SELECT x.Other FROM (SELECT a.Value AS Other FROM Fact a) x",
        ),
        (
            "SELECT a.ID FROM Fact a JOIN Dimension d ON a.DimID=d.ID WHERE d.City=N'杭州'",
            "SELECT b.ID FROM dbo.Fact b JOIN dbo.Dimension c ON b.DimID=c.ID WHERE c.City=N'宁波'",
        ),
        (
            "SELECT a.ID FROM Fact a WHERE EXISTS(SELECT 1 FROM Other b WHERE b.ID=a.ID)",
            "SELECT x.ID FROM Fact x WHERE EXISTS(SELECT 2 FROM Other y WHERE y.ID=x.ID)",
        ),
        (
            "SELECT Value AS label FROM Fact ORDER BY label",
            "SELECT Value AS renamed FROM Fact ORDER BY renamed",
        ),
    ],
)
def test_relational_and_output_aliases_and_values_do_not_change_shape(first, second):
    assert sql_template_fingerprint(first) == sql_template_fingerprint(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("SELECT Value FROM Fact", "SELECT Value FROM Other"),
        ("SELECT Value FROM dbo.Fact", "SELECT Value FROM reporting.Fact"),
        (
            "SELECT a.ID FROM Fact a JOIN Dimension d ON a.DimID=d.ID",
            "SELECT a.ID FROM Fact a JOIN Dimension d ON a.ID=d.ID",
        ),
        (
            "SELECT a.ID FROM Fact a JOIN Fact b ON a.ParentID=b.ID",
            "SELECT a.ID FROM Fact a JOIN Fact b ON b.ParentID=a.ID",
        ),
        (
            "WITH x AS (SELECT Value AS v FROM Fact) SELECT v FROM x",
            "WITH x AS (SELECT Value AS v FROM Other) SELECT v FROM x",
        ),
        ("SELECT Value FROM Fact", "SELECT SUM(Value) FROM Fact"),
    ],
)
def test_physical_sources_and_association_structure_are_not_merged(first, second):
    assert sql_template_fingerprint(first) != sql_template_fingerprint(second)
