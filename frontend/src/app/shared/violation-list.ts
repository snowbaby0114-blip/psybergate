import { Component, input } from '@angular/core';

import { Violation } from '../api/models';
import { describeContext } from './violations';

/**
 * Every violation of a rejection: the message to read, the rule code as a badge, and the machine-readable
 * context folded away in a "Details" element. The context is never needed to understand the message.
 */
@Component({
  selector: 'app-violation-list',
  template: `
    <ul class="violations">
      @for (violation of violations(); track $index) {
        <li>
          <span class="violation-message">{{ violation.message }}</span>
          <code class="badge badge-code">{{ violation.code }}</code>
          @if (entries(violation).length > 0) {
            <details>
              <summary>Details</summary>
              <dl class="facts">
                @for (entry of entries(violation); track entry.label) {
                  <div>
                    <dt>{{ entry.label }}</dt>
                    <dd>{{ entry.value }}</dd>
                  </div>
                }
              </dl>
            </details>
          }
        </li>
      }
    </ul>
  `,
})
export class ViolationList {
  readonly violations = input.required<readonly Violation[]>();

  protected entries(violation: Violation) {
    return describeContext(violation.context);
  }
}
