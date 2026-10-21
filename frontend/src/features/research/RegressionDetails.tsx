import { readRegressionDetails, type RegressionDetailFact } from "../../data/research";
import styles from "./Research.module.css";

function InputFact({ fact }: { fact: RegressionDetailFact }) {
  return <>
    <strong>{fact.metric}: {fact.value} {fact.unit}</strong>
    <div>{fact.period_start ?? "Start date not specified"} to {fact.period_end ?? "End date not specified"}</div>
    <div>{fact.basis ?? "Basis not specified"}</div>
  </>;
}

export function RegressionDetails({ value }: { value: unknown }) {
  if (value === undefined) return null;
  const details = readRegressionDetails(value);
  if (!details) return <p className={styles.hint}>Sample details are temporarily unavailable; reload the research record. The method was not changed and missing data was not filled in.</p>;
  return <details className={styles.details} data-regression-details>
    <summary>View {details.sample_count} fitted samples and method</summary>
    <p>{details.method}: {details.equation}. Y lags X by {details.lag_years} years.</p>
    <p>{details.y_transform === "percent_change"
      ? "Y is the year-over-year growth rate: (current-period raw value − prior-year raw value) ÷ absolute value of the prior-year raw value × 100%. The raw values used in the calculation are shown below."
      : "X and Y use the fact values below directly, without a growth-rate transformation."}</p>
    <ol aria-label="Actual regression samples for the confirmed method">
      {details.samples.map((sample, index) => <li key={index}>
        <dl className={styles.scopeGrid}>
          <dt>X input</dt><dd><InputFact fact={sample.x} /></dd>
          <dt>Y current-period input</dt><dd><InputFact fact={sample.y} /></dd>
          {sample.y_prior && <><dt>Y prior-year input</dt><dd><InputFact fact={sample.y_prior} /></dd></>}
        </dl>
      </li>)}
    </ol>
    <ul>{details.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul>
  </details>;
}
