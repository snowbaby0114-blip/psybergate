import { Component, computed, input } from '@angular/core';

import { RuleOut, RuleStatus } from '../api/models';
import { formatInstantSast, previousDay } from '../shared/time';

const STATUS_LABEL: Record<RuleStatus, string> = {
  in_force: 'In force',
  scheduled: 'Scheduled',
  expired: 'Expired',
};

/**
 * A medicine's rule history, oldest first. The in force / scheduled / expired badge is whatever the
 * server says: deciding what "today" is belongs to the server (a SAST business date), not to the browser.
 */
@Component({
  selector: 'app-rule-timeline',
  templateUrl: './rule-timeline.html',
})
export class RuleTimeline {
  readonly rules = input.required<readonly RuleOut[]>();

  protected readonly ordered = computed(() =>
    [...this.rules()].sort((a, b) => a.effective_from.localeCompare(b.effective_from)),
  );
  protected readonly statusLabel = STATUS_LABEL;
  /** `effective_to` is exclusive, so the last day a rule applies is the day before it. */
  protected readonly lastDay = previousDay;
  protected readonly formatInstant = formatInstantSast;
}
