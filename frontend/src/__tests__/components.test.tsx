import { render, screen } from "@testing-library/react";
import { BandBadge, ReasonList, ScoreBar } from "../components/fraud";
import { money, pct, slaText } from "../lib/format";

describe("format helpers", () => {
  it("describes SLA deadlines in both directions", () => {
    const now = new Date("2026-11-01T12:00:00Z");
    expect(slaText("2026-11-01T15:00:00Z", now)).toBe("3h left");
    expect(slaText("2026-11-01T11:30:00Z", now)).toBe("30m overdue");
    expect(slaText("2026-11-05T12:00:00Z", now)).toBe("4d left");
  });

  it("formats money and percentages, with a dash for missing values", () => {
    expect(money(1234.5)).toContain("1,234.50");
    expect(pct(0.1234, 1)).toBe("12.3%");
    expect(pct(null)).toBe("—");
  });
});

describe("fraud components", () => {
  it("labels every reason with its source so model output is never mistaken for a rule", () => {
    render(
      <ReasonList
        reasons={[
          { source: "model", text: "6 earlier transactions in 10 minutes", contribution: 1.2, label: "Transaction velocity" },
          { source: "rule", text: "Implied travel speed 5,000 km/h", rule: "IMPOSSIBLE_TRAVEL", min_band: "high" },
        ]}
      />,
    );
    const items = screen.getAllByTestId("reason");
    expect(items[0]).toHaveTextContent("Model");
    expect(items[0]).toHaveTextContent("raises risk");
    expect(items[1]).toHaveTextContent("Rule");
    expect(items[1]).toHaveTextContent("IMPOSSIBLE_TRAVEL → at least high");
  });

  it("says so when there are no risk indicators", () => {
    render(<ReasonList reasons={[]} />);
    expect(screen.getByText(/no notable risk indicators/i)).toBeInTheDocument();
  });

  it("shows 'rules only' when no model score exists and unscored bands", () => {
    render(<><ScoreBar score={null} /><BandBadge band={null} /><BandBadge band="critical" /></>);
    expect(screen.getByText("rules only")).toBeInTheDocument();
    expect(screen.getByText("unscored")).toBeInTheDocument();
    expect(screen.getByText("critical")).toBeInTheDocument();
  });
});
