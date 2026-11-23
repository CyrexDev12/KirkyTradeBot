function mcComparison(g) {
  const mc = g.monte_carlo?.home_win_given_no_tie;
  const existing = g.prediction?.home_probability;
  return mc == null || existing == null ? null : mc - existing;
}

function mcRow(label, value) {
  return `<div class="validation-row"><span>${safe(label)}</span><b>${safe(value)}</b></div>`;
}

function mcCompare(g) {
  const f = g.monte_carlo;
  const row = (label, legacy, mc) => `<div class="compare-row"><span>${legacy}</span><label>${label}</label><span>${mc}</span></div>`;
  return `<div class="mc-compare"><div class="compare-head"><span>EXISTING</span><span></span><span>MONTE CARLO</span></div>
    ${row('Home win given no tie', pct(g.prediction?.home_probability), pct(f?.home_win_given_no_tie))}
    ${row('Expected total', points(g.total_prediction?.predicted_total), points(f?.distributions?.total.mean))}
    ${mcRow('Home win difference · MC minus existing', pp(mcComparison(g)))}</div>`;
}

function mcForecastBlock(g) {
  const f = g.monte_carlo;
  if (!f || f.status !== 'available') {
    return `<div class="total-panel"><div class="total-panel-head">Current-season Monte Carlo</div>
      <p class="detail-note">${safe(f?.unavailable_reason || 'Loading forecast…')}</p>
      ${f?.team_game_counts ? `<small>${safe(g.away)} ${f.team_game_counts[g.away]} GP · ${safe(g.home)} ${f.team_game_counts[g.home]} GP · ${f.residual_pair_count} error pairs</small>` : ''}</div>`;
  }
  const d = f.distributions;
  const row = (label, away, home) => `<div class="metric-row"><span class="metric-label">${label}</span><b>${away}</b><b>${home}</b></div>`;
  return `<div class="game-metrics">
    ${row('Full game · incl. OT', safe(g.away), safe(g.home))}
    ${row('Expected points', points(d.away.mean), points(d.home.mean))}
    ${row('Win probability', pct(f.probability_away_win), pct(f.probability_home_win))}
    </div><div class="total-panel"><div class="total-panel-head"><span>EXPECTED TOTAL</span><b>${points(d.total.mean)} pts</b></div>
    <div class="total-panel-sub">80% of simulated totals · ${d.total.interval_80.join('–')} pts</div>
    ${mcRow('Final tie', pct(f.probability_tie))}</div>`;
}

function mcCard(g) {
  const f = g.monte_carlo;
  return `<article class="game-card"><div class="game-card-top"><span class="game-meta"><b>W${g.week}</b>${safe(onlyDate(g.kickoff))}</span>
    <span class="status ${safe(g.status.toLowerCase())}">${safe(g.status)}</span></div>
    <div class="teams">${teamBlock(g.away_team,'away')}<span class="at">@</span>${teamBlock(g.home_team,'home')}</div>
    ${mcForecastBlock(g)}${state.model === 'compare' ? mcCompare(g) : ''}
    <div class="game-card-bottom"><span class="mc-caption">${f?.limited_sample ? 'Limited sample' : 'Experimental MC'}</span>
    <button class="detail-link" data-game="${safe(g.game_id)}">Simulation details ↗</button></div></article>`;
}

function mcTable(games) {
  const compare = state.model === 'compare';
  return `<div class="table-wrap"><table class="game-table"><thead><tr><th>Matchup</th><th>Kickoff</th><th>Away / home pts</th>
    <th>Away / home / tie</th><th>Total · 80% interval</th>${compare ? '<th>Home given no tie · existing / MC</th><th>Difference</th>' : ''}
    <th>Evidence</th><th></th></tr></thead><tbody>${games.map(g => {
      const f = g.monte_carlo, d = f?.distributions;
      return `<tr><td>${safe(g.away)} @ ${safe(g.home)}</td><td>${safe(onlyDate(g.kickoff))}</td><td>${points(d?.away.mean)} / ${points(d?.home.mean)}</td>
        <td>${pct(f?.probability_away_win)} / ${pct(f?.probability_home_win)} / ${pct(f?.probability_tie)}</td>
        <td>${points(d?.total.mean)} · ${d?.total.interval_80.join('–') || '—'}</td>
        ${compare ? `<td>${pct(g.prediction?.home_probability)} / ${pct(f?.home_win_given_no_tie)}</td><td>${pp(mcComparison(g))}</td>` : ''}
        <td>${safe(f?.unavailable_reason || (f?.limited_sample ? 'Limited sample' : 'Current season'))}</td>
        <td><button class="table-detail" data-game="${safe(g.game_id)}">Details ↗</button></td></tr>`;
    }).join('')}</tbody></table></div>`;
}

function mcHistogram(summary, line, title) {
  const hist = summary.histogram;
  const min = Math.floor(Math.min(hist[0][0], line ?? Infinity)), max = Math.ceil(Math.max(hist.at(-1)[0], line ?? -Infinity));
  const unit = 500/(max-min+1), x = n => 10+(n-min+.5)*unit;
  const peak = Math.max(...hist.map(b => b[1])), base = 96;
  const bars = hist.map(([n, count]) => {
    const height = 80*count/peak;
    return `<rect x="${x(n)-unit*.45}" y="${base-height}" width="${unit*.9}" height="${height}" fill="${line != null && n < line ? '#7091b6' : '#a5df68'}"><title>${n} points · ${count} draws</title></rect>`;
  });
  const marker = line == null ? '' : `<line x1="${x(line)}" x2="${x(line)}" y1="5" y2="${base}" stroke="#fff" stroke-dasharray="3 2"/>`;
  return `<svg class="mc-chart" viewBox="0 0 520 118" role="img" aria-label="${safe(title)} histogram${line == null ? '' : ' with line at '+line}">
    ${bars.join('')}${marker}<text x="10" y="113">${min}</text><text x="510" y="113" text-anchor="end">${max} pts</text></svg>`;
}

function mcLines(g, selectedLine) {
  const lines = g.mc_values?.totals || [];
  const entry = lines.find(q => q.line === selectedLine) || lines.find(q => q.line === g.totals?.featured_line) || lines[0];
  const select = entry ? `<label class="model-control">Exchange total <select id="mc-line">${lines.map(q =>
    `<option value="${q.line}" ${q === entry ? 'selected' : ''}>${points(q.line)}</option>`).join('')}</select></label>` : '';
  return `${select}${mcHistogram(g.monte_carlo.distributions.total, entry?.line, 'Total points')}
    ${entry ? mcRow('Over / Under '+points(entry.line), pct(entry.probabilities.over)+' / '+pct(entry.probabilities.under))+
      mcRow('Experimental MC net value · Over / Under', money(entry.over?.net_edge)+' / '+money(entry.under?.net_edge))+
      `<p class="detail-note">${entry.status === 'REFERENCE_ONLY' ? 'Reference quote · depth unavailable. ' : ''}${entry.probabilities.boundary ? 'Boundary estimate from finite scenarios. ' : ''}Net value is estimated per contract at the selected size.</p>` :
      '<p class="detail-note">No verified exchange total is available.</p>'}`;
}

function openMcGame(g) {
  const f = g.monte_carlo;
  $('modal').classList.remove('hidden');
  const title = `<div class="modal-kicker">CURRENT-SEASON MONTE CARLO · WEEK ${g.week}</div>
    <h2 class="modal-title">${safe(g.away)} at ${safe(g.home)}</h2><div class="modal-subtitle">${safe(when(g.kickoff))}</div>`;
  if (f?.status !== 'available') {
    $('modal-content').innerHTML = title + mcForecastBlock(g) + (state.model === 'compare' ? mcCompare(g) : '');
    return;
  }
  const away = f.teams[g.away], home = f.teams[g.home];
  const robust = Number.isInteger(home.adjusted_scoring_games) && Number.isInteger(away.adjusted_scoring_games);
  const stats = [['Games','games',0],['Effective games','effective_n',1],['Points scored / game','points_for',1],
    ['Points allowed / game','points_allowed',1],
    [robust ? 'Robust weighted points scored' : 'Weighted points scored','weighted_for',1],
    [robust ? 'Robust weighted points allowed' : 'Weighted points allowed','weighted_allowed',1],
    ['Scoring strength','scoring_strength',1],['Score prevention','score_prevention',1]];
  if (robust) stats.push(['Scoring games adjusted','adjusted_scoring_games',0],
    ['Points allowed games adjusted','adjusted_defensive_games',0]);
  const weights = (team, summary) => `<details><summary>${safe(team)} game weights</summary>${Object.entries(summary.game_weights)
    .map(([id,w])=>mcRow(id,numeric(w,3))).join('')}</details>`;
  const market = g.mc_values?.moneyline;
  $('modal-content').innerHTML = title + mcForecastBlock(g) + (state.model === 'compare' ? mcCompare(g) : '') + `
    <p class="detail-note">Experimental final-score approximation including overtime. Recent games carry more weight. Each draw reuses one earlier game’s paired scoring errors. Rounding can distort the frequency of final ties.</p>
    <div class="detail-section"><h3>Total points distribution</h3><div id="mc-lines">${mcLines(g)}</div>
    <h3>Score margin · home minus away</h3>${mcHistogram(f.distributions.margin,0,'Home score margin')}
    ${mcRow('Expected margin · central 80%',points(f.distributions.margin.mean)+' pts · '+f.distributions.margin.interval_80.join(' to '))}</div>
    <div class="detail-section"><h3>Current-season inputs${f.limited_sample ? ' · Limited sample' : ''}</h3>
    <div class="compare-head"><span>${safe(g.away)}</span><span></span><span>${safe(g.home)}</span></div>
    ${stats.map(([label,k,n])=>compare(label,away[k],home[k],n)).join('')}
    <p class="detail-note">Scoring strength and prevention use all scoreboard points, including defensive and special-teams scores.</p>
    ${weights(g.away,away)}${weights(g.home,home)}
    ${mcRow('Venue', f.neutral_site ? 'Neutral site' : g.home+' at home')}
    ${mcRow('Applied home margin adjustment',points(f.neutral_site ? 0 : f.home_advantage)+' pts')}
    ${mcRow('Forecast cutoff',when(f.as_of))}${mcRow('Schedule cache updated',when(f.source_updated_at))}
    ${mcRow('Earlier game error pairs',f.residual_pair_count+' · '+points(f.residual_effective_n)+' effective')}
    ${mcRow('Simulated games',f.simulation_count.toLocaleString())}${mcRow('Model version',f.model_version)}
    ${mcRow('Draws with a clipped score',pct(f.diagnostics.clipped_draw_rate))}
    ${f.quality_flags.map(flag=>`<p class="detail-note">${safe(flag)}</p>`).join('')}</div>
    <div class="detail-section"><h3>Verified winner contract</h3>${market?.[g.home] ?
      mcRow('Fair payout · '+g.away+' / '+g.home, money(market[g.away]?.fair_value)+' / '+money(market[g.home]?.fair_value))+
      mcRow('Experimental MC net value · away / home', money(market[g.away]?.net_edge)+' / '+money(market[g.home]?.net_edge))+
      '<p class="detail-note">Conditional on full-game settlement. A final tie pays $0.50 per side. Rescheduling terms still apply.</p>' :
      '<p class="detail-note">No verified fixed-payout comparison is available.</p>'}</div>`;
  $('mc-lines').onchange = e => {
    if (e.target.id === 'mc-line') $('mc-lines').innerHTML = mcLines(g, Number(e.target.value));
  };
}

async function showMcValidation() {
  const season = state.season;
  $('modal').classList.remove('hidden');
  $('modal-content').innerHTML = '<div class="loading">Evaluating earlier weeks…</div>';
  try {
    const r = await request('/api/model/monte-carlo-metrics?season='+season);
    const m = r.metrics, paired = r.matched_binary;
    $('modal-content').innerHTML = `<div class="modal-kicker">MONTE CARLO VALIDATION</div><h2 class="modal-title">${season} replay</h2>
      ${mcRow('Eligible / completed games',r.eligible+' / '+r.completed)}
      <p class="detail-note">Each reconstruction uses outcomes available by the previous-week cutoff. The comparison uses matching pregame inputs on the same games.</p>
      ${m ? mcRow('Home win Brier · excluding ties',numeric(m.binary_no_tie?.brier))+
          mcRow('Total MAE · MC / weighted PPG',points(m.score_errors.total.mae)+' / '+points(m.baseline_errors.total.mae)+' pts')+
          mcRow('Observed coverage · 80% total interval',pct(m.intervals.total['80'].coverage))+
          mcRow('Final tie rate · simulated / observed',pct(m.predicted_tie_rate)+' / '+pct(m.observed_tie_rate)) : 'Insufficient current-season data'}
      ${paired ? mcRow('Matched binary games',paired.mc.games)+mcRow('Brier · MC / existing',numeric(paired.mc.brier)+' / '+numeric(paired.existing.brier)) : ''}
      ${r.matched_totals ? mcRow('Matched total MAE · MC / existing',points(r.matched_totals.mc.mae)+' / '+points(r.matched_totals.existing.mae)+' pts') : ''}`;
  } catch (err) {
    $('modal-content').textContent = 'Validation unavailable. '+err.message;
  }
}
