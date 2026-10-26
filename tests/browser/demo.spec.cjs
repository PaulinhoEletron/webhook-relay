const { test, expect } = require('@playwright/test');
const pending = {
  run_id: '00000000-0000-4000-8000-000000000007', event_id: '00000000-0000-4000-8000-000000000008',
  status: 'pending', created_at: new Date().toISOString(), expires_at: new Date(Date.now()+3600000).toISOString(),
  attempts_used: 0, attempts: [], delivered_at: null,
};
async function online(page) {
  await page.route('**/health/ready', route => route.fulfill({json:{status:'ready'}}));
  await page.route('**/health/worker', route => route.fulfill({json:{status:'online'}}));
}
async function mockCreation(page) {
  await page.route('**/demo/runs', route => route.fulfill({status:202, json:{...pending, read_token:'test-only-token'}}));
}

test('real deliveries, keyboard access, mobile layout and refresh', async ({page}) => {
  test.setTimeout(90000);
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const urls = []; page.on('request', request => urls.push(request.url()));
  await page.goto('/');
  await expect(page.locator('#health')).toHaveText('API ready · Worker online', {timeout:20000});
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', {name:'Skip to scenarios'})).toBeFocused();
  await page.keyboard.press('Enter');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', {name:/Successful delivery/})).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#status')).toHaveText('Delivered', {timeout:20000});
  await expect(page.locator('#attempts tr')).toHaveCount(1);
  await expect(page.locator('#attempts td').nth(3)).toHaveText('200');
  const successEvent = await page.locator('#event-id').textContent();
  await page.getByRole('button', {name:/Retry & recover/}).click();
  await expect(page.locator('#event-id')).not.toHaveText(successEvent);
  await expect(page.locator('#status')).toHaveText('Delivered', {timeout:25000});
  await expect(page.locator('#attempts tr')).toHaveCount(3);
  expect(await page.locator('#attempts tr td:nth-child(4)').allTextContents()).toEqual(['503','503','200']);
  await page.screenshot({path:'test-results/desktop-retry.png', fullPage:true});
  const retryEvent = await page.locator('#event-id').textContent();
  await page.getByRole('button', {name:/Permanent failure/}).click();
  await expect(page.locator('#event-id')).not.toHaveText(retryEvent);
  await expect(page.locator('#status')).toHaveText('Failed', {timeout:20000});
  await expect(page.locator('#attempts tr')).toHaveCount(1);
  await expect(page.locator('#attempts td').nth(3)).toHaveText('400');
  const polls = urls.filter(url => url.includes('/demo/runs/')).length;
  await page.waitForTimeout(2300);
  expect(urls.filter(url => url.includes('/demo/runs/')).length).toBe(polls);
  await page.getByRole('button', {name:'Refresh status'}).click();
  await expect.poll(() => urls.filter(url => url.includes('/demo/runs/')).length).toBe(polls+1);
  await expect(page.locator('#status')).toHaveText('Failed');
  for (const width of [375, 320]) {
    await page.setViewportSize({width, height:812});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.getByRole('button', {name:/Retry & recover/})).toBeVisible();
  }
  await page.screenshot({path:'test-results/mobile-failure.png', fullPage:true});
  expect(await page.evaluate(() => [localStorage.length,sessionStorage.length])).toEqual([0,0]);
  expect(urls.some(url => /token=|Bearer|test-only-token/.test(url))).toBe(false);
  expect(errors).toEqual([]);
  await page.reload();
  await expect(page.locator('#run')).toBeHidden();
  await expect(page.getByRole('button', {name:'Refresh status'})).toBeDisabled();
  await page.getByRole('link',{name:'Source',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Source preview'})).toBeVisible();
});

test('API unavailable and worker offline are clear and prevent creation', async ({page}) => {
  let creations=0; page.on('request', r => {if(r.method()==='POST') creations++;});
  await page.route('**/health/ready', route => route.fulfill({status:503,json:{}}));
  await page.goto('/');
  await expect(page.locator('#health')).toContainText('Service unavailable');
  await page.getByRole('button', {name:/Successful delivery/}).click();
  await expect(page.locator('#message')).toContainText('Service or worker unavailable');
  await online(page);
  await page.route('**/health/worker', route => route.fulfill({json:{status:'offline'}}));
  await page.getByRole('button', {name:'Check connection'}).click();
  await expect(page.locator('#health')).toContainText('Worker offline');
  await page.getByRole('button', {name:/Successful delivery/}).click();
  await expect(page.locator('#message')).toContainText('Service or worker unavailable');
  expect(creations).toBe(0);
});

test('creation quota and expired-run messages', async ({page}) => {
  await online(page);
  await page.route('**/demo/runs', route => route.fulfill({status:429,headers:{'Retry-After':'37'},json:{}}));
  await page.goto('/');
  await page.getByRole('button', {name:/Successful delivery/}).click();
  await expect(page.locator('#message')).toContainText('Try again in 37 seconds');
  await mockCreation(page);
  await page.route('**/demo/runs/*', route => route.fulfill({status:401,json:{}}));
  await page.getByRole('button', {name:/Successful delivery/}).click();
  await expect(page.locator('#message')).toContainText('expired or is no longer accessible');
  await expect(page.getByRole('button', {name:'Refresh status'})).toBeDisabled();
});

test('polls every two seconds, stops at two minutes, manual refresh is bounded', async ({page}) => {
  await page.clock.install(); await online(page); await mockCreation(page);
  let polls=0;
  await page.route('**/demo/runs/*', route => { polls++; return route.fulfill({json:pending}); });
  await page.goto('/');
  await page.getByRole('button', {name:/Successful delivery/}).click();
  await expect(page.locator('#message')).toContainText('Updates every two seconds');
  expect(polls).toBe(1);
  await page.clock.runFor(1900); expect(polls).toBe(1);
  await page.clock.runFor(100);
  await expect.poll(() => polls).toBe(2);
  await expect(page.locator('#message')).toContainText('Updates every two seconds');
  await page.clock.fastForward(120001);
  await expect(page.locator('#message')).toContainText('Two minutes reached');
  const stopped=polls;
  await page.clock.runFor(5000); expect(polls).toBe(stopped);
  await page.getByRole('button', {name:'Refresh status'}).click();
  await expect.poll(() => polls).toBe(stopped+1);
  await page.clock.runFor(5000); expect(polls).toBe(stopped+1);
});

test('polling failure preserves recorded history and supports recovery', async ({page}) => {
  await online(page); await mockCreation(page);
  await page.route('**/demo/runs/*', route => route.fulfill({json:pending}));
  await page.goto('/');
  await page.getByRole('button', {name:/Successful delivery/}).click();
  await expect(page.locator('#status')).toHaveText('Pending');
  await page.route('**/demo/runs/*', route => route.fulfill({status:503,json:{}}));
  await page.getByRole('button', {name:'Refresh status'}).click();
  await expect(page.locator('#message')).toContainText('Your last recorded history');
  await expect(page.locator('#status')).toHaveText('Pending');
  await page.route('**/demo/runs/*', route => route.fulfill({status:429,headers:{'Retry-After':'12'},json:{}}));
  await page.getByRole('button', {name:'Refresh status'}).click();
  await expect(page.locator('#message')).toContainText('Try again in 12 seconds');
});
