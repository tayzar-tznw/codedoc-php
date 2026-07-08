<?php
declare(strict_types=1);

namespace Billing\Model\Behavior;

use Cake\ORM\Behavior;

class AuditBehavior extends Behavior
{
    public function auditTrail(int $id): array
    {
        return ['id' => $id, 'events' => []];
    }
}
