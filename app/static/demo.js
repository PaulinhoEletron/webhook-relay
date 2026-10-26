'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const buttons = [...document.querySelectorAll('[data-scenario]')];
  const labels = {pending:'Pending', processing:'Processing', retry_wait:'Waiting to retry', delivered:'Delivered', failed:'Failed'};
  let run = null, generation = 0, timer = null, deadline = 0, busy = false;
  function message(text, error = false) { $('message').textContent = text; $('message').dataset.kind = error ? 'error' : 'info'; }
  function stop() { clearTimeout(timer); timer = null; }
  function controls(value) { busy = value; buttons.forEach(button => button.disabled = value); $('refresh').disabled = value || !run; }
  async function request(path, options = {}) {
    const response = await fetch(path, {...options, cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) { const error = new Error('Request failed'); error.status = response.status; error.retry = Math.max(1, Number(response.headers.get('Retry-After')) || 60); throw error; }
    return response.json();
  }
  async function health() {
    try {
      await request('/health/ready');
      const worker = await request('/health/worker');
      $('health').textContent = worker.status === 'online' ? 'API ready · Worker online' : 'Worker offline · Delivery is paused';
      return worker.status === 'online';
    } catch { $('health').textContent = 'Service unavailable · Check connection'; return false; }
  }
  function failure(error, creating = false) {
    stop();
    if (error.status === 429) message(`Sandbox limit reached. Try again in ${error.retry} seconds.`, true);
    else if (error.status === 401 || error.status === 404) {
      if (creating) message('The demo is unavailable on this server.', true);
      else { run = null; message('This run has expired or is no longer accessible. Start a new scenario.', true); }
    } else message(creating ? 'Could not confirm creation. The service may be unavailable. Check connection before trying again.' : 'Service unavailable. Your last recorded history is shown below. Refresh status to try again.', true);
  }
  function render(data) {
    $('empty').hidden = true; $('run').hidden = false;
    $('event-id').textContent = data.event_id;
    $('status').textContent = labels[data.status] || data.status;
    $('attempt-count').textContent = `${data.attempts_used} / 5`;
    const last = data.attempts.at(-1);
    const end = data.delivered_at || (data.status === 'failed' ? last?.ended_at : null);
    $('elapsed').textContent = `${Math.max(0, ((end ? Date.parse(end) : Date.now()) - Date.parse(data.created_at)) / 1000).toFixed(1)} s`;
    $('expiry').textContent = `Run access expires at ${new Date(data.expires_at).toLocaleTimeString()}. Reloading clears this tab’s access.`;
    const rows = data.attempts.map(attempt => {
      const row = document.createElement('tr');
      [attempt.attempt_number, new Date(attempt.started_at).toLocaleTimeString(), attempt.outcome.replaceAll('_', ' '), attempt.http_status ?? '—', attempt.duration_ms == null ? '—' : `${attempt.duration_ms} ms`].forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); });
      return row;
    });
    if (!rows.length) { const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 5; cell.textContent = 'Saved. Waiting for the worker’s first attempt.'; row.append(cell); rows.push(row); }
    $('attempts').replaceChildren(...rows);
  }
  async function update(id, automatic) {
    if (!run || id !== generation) return;
    try {
      const current = run;
      const data = await request(`/demo/runs/${current.run_id}`, {headers:{Authorization:`Bearer ${current.read_token}`}});
      if (id !== generation) return;
      render(data);
      if (['delivered','failed'].includes(data.status)) {
        stop(); message(data.status === 'delivered' ? 'Delivered. The receiver accepted the event.' : 'Delivery stopped. This scenario demonstrates a permanent rejection.'); return;
      }
      if (!automatic || Date.now() >= deadline) { stop(); message('Automatic updates paused. Refresh status for the latest recorded result.'); return; }
      const online = await health();
      if (id !== generation) return;
      if (!online) { stop(); message('Delivery is paused or unavailable. Your event is saved. Check connection and refresh status.', true); return; }
      message('Following real delivery attempts. Updates every two seconds.');
      timer = setTimeout(() => { if (Date.now() >= deadline) { message('Two minutes reached. Refresh status for the latest recorded result.'); stop(); } else update(id, true); }, 2000);
    } catch (error) { if (id === generation) failure(error); }
    finally { if (id === generation) controls(false); }
  }
  async function start(scenario) {
    if (busy) return;
    stop(); const id = ++generation; controls(true);
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.scenario === scenario)));
    message('Checking service before starting…');
    if (!await health()) { if (id === generation) { message('Service or worker unavailable. Check connection before starting a new run.', true); controls(false); } return; }
    try {
      const created = await request('/demo/runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({scenario})});
      if (id !== generation) return;
      run = created; deadline = Date.now() + 120000;
      $('run').hidden = true; $('empty').hidden = true;
      message('Event saved. Waiting for the first recorded result…');
      await update(id, true);
    } catch (error) { if (id === generation) failure(error, true); }
    finally { if (id === generation) controls(false); }
  }
  buttons.forEach(button => { button.setAttribute('aria-pressed','false'); button.addEventListener('click', () => start(button.dataset.scenario)); });
  $('refresh').addEventListener('click', () => { if (busy || !run) return; stop(); controls(true); update(++generation, false); });
  $('check').addEventListener('click', async () => { $('check').disabled = true; await health(); $('check').disabled = false; });
  window.addEventListener('pagehide', () => { stop(); generation++; run = null; });
  health();
})();
