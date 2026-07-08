<?php
declare(strict_types=1);

namespace Billing\Controller\Component;

use Cake\Controller\Component;

class PaymentComponent extends Component
{
    public function capture(int $invoiceId): string
    {
        return 'captured:' . $invoiceId;
    }
}
